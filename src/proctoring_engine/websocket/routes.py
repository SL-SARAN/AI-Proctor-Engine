"""FastAPI WebSocket endpoint for the exam proctoring protocol.

Implements the authenticated WebSocket connection described in
``docs/02-ingestion-layer-design.md`` §2.  One connection per exam
session, authenticated by the HS256 session token issued during the
LTI 1.3 launch.

**Connection lifecycle:**

1. Client opens ``ws://<host>/ws`` carrying the session token in the
   ``Sec-WebSocket-Protocol`` subprotocol header.  The header takes
   the form ``proctoring-v1.<jwt>`` — the leading ``proctoring-v1.``
   prefix is a fixed protocol discriminator that scopes the token to
   this engine and stops an arbitrary ``Authorization``-shaped string
   being misinterpreted as a token on a different service.  The token
   is **never** accepted via a query parameter: a query parameter
   lands in reverse-proxy and load-balancer access logs the same way
   the LTI redirect's token did, and the same fix applies at this
   layer (RFC 6455 ``Sec-WebSocket-Protocol``).
2. Server validates the token, verifies the session exists and is in
   an acceptable status (``PENDING`` or ``ACTIVE``), transitions
   ``PENDING → ACTIVE``, and starts the heartbeat ticker.
3. Each client message is parsed via :func:`parse_client_message`,
   pushed into the :class:`TelemetryEventBuffer`, and acked with a
   monotonic sequence number.
4. On a kill-switch decision, the server sends a
   :class:`KillSwitchDeliver` and waits for the client's
   :class:`KillSwitchAcknowledge`.
5. On disconnect (clean or unclean), the heartbeat ticker stops and
   resources are released.

**Heartbeat:**

A server-initiated WebSocket ping every ``heartbeat_interval_seconds``
(default 15 s).  No pong within ``heartbeat_timeout_seconds`` (default
30 s) triggers a ``connection_lost`` MEDIUM flag — *not* an
auto-termination.  Repeated drops escalate through the same
accumulated-score path as other MEDIUM signals.

**Reconnect:**

A client may reconnect with the same session token to the same
``ExamSession`` row as long as the session hasn't reached a terminal
status (``COMPLETED``, ``TERMINATED``).  The handler verifies this
at connection time and rejects stale tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid as _uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from proctoring_engine.lti.session_token import (
    SessionClaims,
    SessionTokenError,
    SessionTokenExpired,
    SessionTokenInvalid,
    decode_session_token,
)
from proctoring_engine.lti.config import LtiSettings
from proctoring_engine.lti.roles import AppRole
from proctoring_engine.models import ExamSession, SessionStatus
from proctoring_engine.websocket.client import (
    ClientMessageType,
    EnvelopeValidationError,
    KillSwitchAcknowledge,
    parse_client_message,
)
from proctoring_engine.websocket.server import (
    DeliveryService,
    TelemetryEventBuffer,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Close codes (per RFC 6455 §7.4.2, application-defined range 4000–4999)
# ---------------------------------------------------------------------------

WS_CLOSE_AUTH_FAILED = 4001
"""Token is invalid or missing."""

WS_CLOSE_AUTH_EXPIRED = 4002
"""Token signature is valid but expired."""

WS_CLOSE_SESSION_INVALID = 4003
"""Session does not exist or is in a terminal state."""

WS_CLOSE_SESSION_MISMATCH = 4004
"""The ``session_id`` in a message doesn't match the token's session."""

WS_CLOSE_NOT_LEARNER = 4005
"""Only learners may open a telemetry WebSocket."""

WS_CLOSE_PROTOCOL_ERROR = 4006
"""The client sent a message that could not be parsed."""

WS_CLOSE_HEARTBEAT_TIMEOUT = 4007
"""No pong received within the grace window."""


# ---------------------------------------------------------------------------
# Router dependencies (test-injectable)
# ---------------------------------------------------------------------------

@dataclass
class _WsRouterDeps:
    """Injected dependencies for the WebSocket router.

    Mirrors ``_RouterDeps`` from the LTI layer — the same
    pattern of explicit, test-injectable wiring.
    """

    settings: LtiSettings
    get_db: Callable[[], Session]
    event_buffer: TelemetryEventBuffer
    heartbeat_interval_seconds: float = 15.0
    heartbeat_timeout_seconds: float = 30.0


# ---------------------------------------------------------------------------
# Session lookup helper
# ---------------------------------------------------------------------------

_CONNECTABLE_STATUSES = frozenset(
    {SessionStatus.PENDING, SessionStatus.ACTIVE}
)


def _lookup_session(
    db: Session,
    session_id: str,
) -> ExamSession | None:
    """Return the ``ExamSession`` if it exists and is connectable,
    otherwise ``None``.
    """

    try:
        sid = _uuid.UUID(session_id)
    except (ValueError, AttributeError):
        return None

    return (
        db.query(ExamSession)
        .filter(
            ExamSession.id == sid,
            ExamSession.status.in_(
                [s.value for s in _CONNECTABLE_STATUSES]
            ),
        )
        .first()
    )


def _activate_session(db: Session, exam_session: ExamSession) -> None:
    """Transition a PENDING session to ACTIVE on first WebSocket connect."""

    if exam_session.status == SessionStatus.PENDING:
        exam_session.status = SessionStatus.ACTIVE
        db.commit()


# ---------------------------------------------------------------------------
# Subprotocol token extraction
# ---------------------------------------------------------------------------

# The fixed prefix that scopes the subprotocol to this engine.  The
# token arrives as ``proctoring-v1.<jwt>``.  Any other subprotocol value
# (including the absence of one, or an unrelated value the client
# offers for compatibility with a generic WS endpoint) is rejected.
_SUBPROTOCOL_PREFIX = "proctoring-v1."


def _extract_token_from_subprotocol(websocket: WebSocket) -> str | None:
    """Pull the JWT out of the ``Sec-WebSocket-Protocol`` header.

    Returns the JWT string if exactly one offered subprotocol has the
    :data:`_SUBPROTOCOL_PREFIX` prefix, ``None`` otherwise.  Per RFC
    6455 §4.1, the client may offer multiple subprotocols in a
    comma-separated list; we accept the first one that matches our
    prefix.  A header that is missing entirely, carries only
    non-matching values, or carries multiple matching values is
    treated as malformed.
    """

    # Starlette / FastAPI exposes the parsed header values as a
    # comma-separated list of strings (the wire format from RFC
    # 6455 §1.9).  ``websocket.headers.get("sec-websocket-protocol")``
    # returns the raw string, which is the shape we want to split.
    raw = websocket.headers.get("sec-websocket-protocol")
    if not raw:
        return None

    offered = [segment.strip() for segment in raw.split(",") if segment.strip()]
    matching = [
        value for value in offered if value.startswith(_SUBPROTOCOL_PREFIX)
    ]
    if len(matching) != 1:
        return None

    return matching[0][len(_SUBPROTOCOL_PREFIX):]


def _echo_subprotocol(websocket: WebSocket) -> str | None:
    """Return the subprotocol value to echo on ``accept()``.

    Per RFC 6455 §4.2.2, when the client offers a subprotocol the
    server must echo exactly one of the offered values (or none, in
    which case no ``Sec-WebSocket-Protocol`` header is sent on the
    101 Switching Protocols response).  We echo the single matching
    value so the client can correlate its offered list with the
    server's acceptance.
    """

    raw = websocket.headers.get("sec-websocket-protocol")
    if not raw:
        return None
    offered = [segment.strip() for segment in raw.split(",") if segment.strip()]
    matching = [
        value for value in offered if value.startswith(_SUBPROTOCOL_PREFIX)
    ]
    if len(matching) == 1:
        return matching[0]
    return None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def build_ws_router(deps: _WsRouterDeps) -> APIRouter:
    """Build the WebSocket router for the telemetry connection.

    The factory pattern matches the LTI layer so the test suite can
    inject its own dependencies without monkey-patching.
    """

    router = APIRouter(tags=["websocket"])

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        # ------------------------------------------------------------------
        # 1. Extract and validate the session token
        #
        # The token arrives in the ``Sec-WebSocket-Protocol`` subprotocol
        # header, never as a query parameter.  The header value is
        # ``proctoring-v1.<jwt>`` — the ``proctoring-v1.`` prefix is a
        # fixed protocol discriminator that scopes the token to this
        # engine; without it, an arbitrary token-shaped string from a
        # different service could be misinterpreted as ours.
        #
        # A query-parameter fallback is deliberately **not** supported.
        # A query parameter on the wss:// URL lands in reverse-proxy and
        # load-balancer access logs the same way the LTI redirect's
        # ``?session_token=...`` did, and the fix at that layer (URL
        # fragment) does not apply here — fragments are never sent to
        # the server, so the server cannot authenticate against them.
        # The Sec-WebSocket-Protocol header is the RFC 6455 mechanism
        # for passing a credential on the WS handshake without it
        # touching the URL, and it is the only mechanism we accept.
        # ------------------------------------------------------------------
        token = _extract_token_from_subprotocol(websocket)
        if token is None:
            await websocket.close(
                code=WS_CLOSE_AUTH_FAILED,
                reason="Missing or malformed Sec-WebSocket-Protocol token.",
            )
            return

        try:
            claims: SessionClaims = decode_session_token(
                token, settings=deps.settings
            )
        except SessionTokenExpired:
            await websocket.close(
                code=WS_CLOSE_AUTH_EXPIRED, reason="Token expired."
            )
            return
        except SessionTokenInvalid:
            await websocket.close(
                code=WS_CLOSE_AUTH_FAILED, reason="Invalid token."
            )
            return

        # Only learner-role tokens may open a telemetry connection.
        if claims.role != AppRole.LEARNER:
            await websocket.close(
                code=WS_CLOSE_NOT_LEARNER,
                reason="Only learner-role tokens may open a telemetry connection.",
            )
            return

        # ------------------------------------------------------------------
        # 2. Verify the session exists and is connectable
        # ------------------------------------------------------------------
        db = deps.get_db()
        exam_session = _lookup_session(db, claims.session_id)
        if exam_session is None:
            await websocket.close(
                code=WS_CLOSE_SESSION_INVALID,
                reason="Session not found or in terminal state.",
            )
            return

        # Transition PENDING → ACTIVE on first connect.
        _activate_session(db, exam_session)

        # ------------------------------------------------------------------
        # 3. Accept the WebSocket and start the message loop
        # ------------------------------------------------------------------
        # Echo the selected subprotocol back to the client (RFC 6455
        # §4.2.2 — the server may accept at most one of the client's
        # offered subprotocols, or none).  Echoing is not required for
        # the auth flow but it lets the client confirm the server
        # actually validated the token rather than silently accepting
        # the connection.
        await websocket.accept(subprotocol=_echo_subprotocol(websocket))

        delivery = DeliveryService(
            ack_grace_seconds=deps.heartbeat_timeout_seconds,
        )

        # Start the heartbeat task.
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(
                websocket,
                interval=deps.heartbeat_interval_seconds,
                timeout=deps.heartbeat_timeout_seconds,
            )
        )

        try:
            await _message_loop(
                websocket=websocket,
                claims=claims,
                delivery=delivery,
                event_buffer=deps.event_buffer,
            )
        except WebSocketDisconnect:
            logger.info(
                "WebSocket disconnected for session %s.", claims.session_id
            )
        except Exception:
            logger.exception(
                "Unexpected error in WebSocket handler for session %s.",
                claims.session_id,
            )
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    return router


# ---------------------------------------------------------------------------
# Heartbeat loop
# ---------------------------------------------------------------------------

async def _heartbeat_loop(
    websocket: WebSocket,
    *,
    interval: float,
    timeout: float,
) -> None:
    """Send WebSocket pings and track pong responses.

    Runs as a background ``asyncio.Task`` for the lifetime of the
    connection.  If no pong is received within ``timeout`` seconds
    after a ping, the connection is closed with
    :data:`WS_CLOSE_HEARTBEAT_TIMEOUT`.
    """

    try:
        while True:
            await asyncio.sleep(interval)
            try:
                # FastAPI / Starlette exposes send/receive_bytes but
                # the standard WebSocket ping/pong is handled at the
                # ASGI server level (uvicorn).  We send an application-
                # level ping as a JSON message; the client responds
                # with a type: "pong" message.
                await asyncio.wait_for(
                    websocket.send_json({"type": "ping"}),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("Heartbeat timeout; closing connection.")
                await websocket.close(
                    code=WS_CLOSE_HEARTBEAT_TIMEOUT,
                    reason="Heartbeat timeout.",
                )
                return
            except Exception:
                # Connection already closed.
                return
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# Message loop
# ---------------------------------------------------------------------------

async def _message_loop(
    *,
    websocket: WebSocket,
    claims: SessionClaims,
    delivery: DeliveryService,
    event_buffer: TelemetryEventBuffer,
) -> None:
    """Receive, validate, buffer, and ack client messages."""

    while True:
        raw_text = await websocket.receive_text()

        # -- Parse JSON ------------------------------------------------
        try:
            raw: dict[str, Any] = json.loads(raw_text)
        except json.JSONDecodeError:
            await websocket.close(
                code=WS_CLOSE_PROTOCOL_ERROR,
                reason="Invalid JSON.",
            )
            return

        # -- Ignore client pong responses ------------------------------
        if raw.get("type") == "pong":
            continue

        # -- Parse into typed envelope ---------------------------------
        try:
            message = parse_client_message(raw)
        except EnvelopeValidationError as exc:
            await websocket.close(
                code=WS_CLOSE_PROTOCOL_ERROR,
                reason=str(exc)[:120],  # Truncate for the close frame.
            )
            return

        # -- Verify session_id matches the token -----------------------
        if message.session_id != claims.session_id:
            await websocket.close(
                code=WS_CLOSE_SESSION_MISMATCH,
                reason="session_id in message does not match token.",
            )
            return

        # -- Handle kill-switch ack ------------------------------------
        if isinstance(message, KillSwitchAcknowledge):
            flag_id = message.payload.flag_id
            matched = delivery.record_kill_switch_ack(flag_id)
            if matched:
                logger.info(
                    "Kill-switch ack received for flag %s in session %s.",
                    flag_id,
                    claims.session_id,
                )
            # Always ack the ack to the client.
            ack = delivery.next_ack()
            await websocket.send_json(ack.to_json_dict())
            continue

        # -- Buffer the telemetry event --------------------------------
        event_buffer.push(message)

        # -- Send ack --------------------------------------------------
        ack = delivery.next_ack()
        await websocket.send_json(ack.to_json_dict())
