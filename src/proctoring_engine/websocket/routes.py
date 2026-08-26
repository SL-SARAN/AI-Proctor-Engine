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
from sqlalchemy import func
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
from proctoring_engine.models import DeliveryStatus, ExamSession, SessionStatus
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

WS_CLOSE_IDENTITY_BACKEND_UNAVAILABLE = 4008
"""Identity backend could not be constructed and no valid override exists."""


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

        # ------------------------------------------------------------------
        # 3. Accept the WebSocket before attempting identity verification
        # ------------------------------------------------------------------
        # Echo the selected subprotocol back to the client (RFC 6455
        # §4.2.2 — the server may accept at most one of the client's
        # offered subprotocols, or none).  Echoing is not required for
        # the auth flow but it lets the client confirm the server
        # actually validated the token rather than silently accepting
        # the connection.
        await websocket.accept(subprotocol=_echo_subprotocol(websocket))

        # ------------------------------------------------------------------
        # 4. Attempt identity backend construction after WebSocket accept
        # ------------------------------------------------------------------
        from proctoring_engine.inference.identity_match import (
            FaceRecognitionBackend,
        )
        from proctoring_engine.models import (
            IdentityVerificationStatus,
            IdentityVerificationOverrideRequest,
            OverrideRequestStatus,
        )
        from sqlalchemy import select

        identity_backend_available = False
        valid_override_exists = False
        identity_backend_unavailable = False  # Signal to dispatcher

        try:
            # Attempt to construct the identity backend
            FaceRecognitionBackend()
            identity_backend_available = True
        except ImportError:
            # face_recognition library not available
            identity_backend_available = False
            # Check for valid approved override
            stmt = select(IdentityVerificationOverrideRequest).where(
                IdentityVerificationOverrideRequest.exam_session_id == exam_session.id,
                IdentityVerificationOverrideRequest.status == OverrideRequestStatus.APPROVED,
                IdentityVerificationOverrideRequest.valid_from <= func.now(),
                IdentityVerificationOverrideRequest.valid_until >= func.now(),
            )
            override_result = db.execute(stmt).scalar_one_or_none()
            valid_override_exists = override_result is not None

        # Handle the three cases based on identity backend availability and override status
        if identity_backend_available:
            # Case 1: Backend available - proceed normally
            _activate_session(db, exam_session)
            # Note: identity_verification_status will be set to PENDING_CHECK by default
            # and updated to VERIFIED/FAILED_TO_MATCH by the aggregator later
        elif valid_override_exists:
            # Case 2: Backend unavailable but valid override exists
            exam_session.identity_verification_status = IdentityVerificationStatus.UNAVAILABLE
            _activate_session(db, exam_session)
            # Signal to the dispatcher that identity backend is unavailable
            # so it emits the mandatory-review flag
            identity_backend_unavailable = True
        else:
            # Case 3: Backend unavailable and no valid override
            # Transition session to TERMINATED with specific reason
            exam_session.status = SessionStatus.TERMINATED
            # We need to add a termination reason column or use existing mechanism
            # Looking at TerminationRecord, it has a 'reason' field
            # But we need to create a TerminationRecord entry
            from proctoring_engine.models import TerminationRecord
            termination_record = TerminationRecord(
                exam_session_id=exam_session.id,
                triggering_flag_id=None,  # No flag triggered this termination
                reason="identity_backend_unavailable_no_override",
                client_delivery_status=DeliveryStatus.SENT,  # We're about to send the close
                lms_delivery_status=DeliveryStatus.SENT,
            )
            db.add(termination_record)
            db.commit()

            # Structured observability log line at INFO level.
            #
            # This is the one explicit signal that fires when this path
            # is hit.  It is distinguishable from generic termination
            # messages because it carries the specific ``reason``
            # field — searchable and filterable in the standard
            # platform log aggregator.  No metrics or alerting
            # pipeline is built here; that work is tracked under
            # DEPLOYMENT.md §7 (observability), which remains
            # unimplemented.
            logger.warning(
                "termination.session_blocked session_id=%s reason=%s "
                "detail=%s",
                str(exam_session.id),
                "identity_backend_unavailable_no_override",
                "identity backend could not be constructed and no "
                "IdentityVerificationOverrideRequest covers this session",
            )

            # Close WebSocket with specific close code
            await websocket.close(
                code=WS_CLOSE_IDENTITY_BACKEND_UNAVAILABLE,
                reason="Identity backend unavailable and no valid override exists.",
            )
            return

        delivery = DeliveryService(
            ack_grace_seconds=deps.heartbeat_timeout_seconds,
        )

        # ------------------------------------------------------------------
        # 3a. Spin up the dispatcher + persistence worker
        # ------------------------------------------------------------------
        # The FrameDispatcher is per-session.  It runs in its own thread,
        # draining the shared TelemetryEventBuffer, invoking the
        # inference runners, and pushing FlagDecision objects to the
        # FlagPersistenceWorker.  The persistence worker then writes
        # the Flag + TelemetryEvent rows to Postgres and fires the
        # kill-switch callback if a CRITICAL flag with
        # triggered_termination=True was raised.
        from proctoring_engine.fusion.aggregator import (
            PolicySnapshot,
            SessionContext,
        )
        from proctoring_engine.orchestration._flag_persistence_worker import (
            FlagPersistenceWorker,
        )
        from proctoring_engine.orchestration._frame_dispatcher import (
            FrameDispatcher,
            FrameDispatcherConfig,
        )

        policy_snapshot = PolicySnapshot(
            terminate_on_second_face=True,
            second_face_confirmation_frames=3,
            gaze_min_duration_ms=800,
            gaze_window_seconds=300,
            gaze_warning_limit=3,
            gaze_termination_limit=8,
            medium_score_termination_threshold=10.0,
            medium_score_action="auto_terminate",
            liveness_check_enabled=False,
            liveness_check_action=None,
            liveness_score_threshold=0.5,
            liveness_confirmation_frames=3,
            identity_similarity_threshold=0.6,
            identity_confirmation_frames=3,
            audio_noise_floor_dbfs=-30.0,
            audio_speech_ratio_threshold=0.3,
        )
        context = SessionContext(
            exam_session_id=_uuid.UUID(claims.session_id),
            participant_id=exam_session.participant_id,
            exam_reference=exam_session.exam_reference,
            policy_config_id=exam_session.policy_config_id,
        )
        dispatcher_config = FrameDispatcherConfig(
            policy_snapshot=policy_snapshot,
            context=context,
            identity_backend_unavailable=identity_backend_unavailable,
        )
        dispatcher = FrameDispatcher(
            config=dispatcher_config,
            event_buffer=deps.event_buffer,
        )

        # The kill-switch callback runs on the persistence-worker
        # thread; it must hand the kill-switch back to the asyncio
        # loop without awaiting (no event loop on that thread).
        # ``loop.call_soon_threadsafe`` enqueues the delivery on the
        # asyncio loop running the WS handler.  ``_kill_switch_queue``
        # is drained by the ``_kill_switch_drain_loop`` task.
        import asyncio as _asyncio

        loop = _asyncio.get_running_loop()
        kill_switch_queue: _asyncio.Queue = _asyncio.Queue()

        def _on_kill_switch(flag_id: str, reason: str) -> None:
            try:
                loop.call_soon_threadsafe(
                    kill_switch_queue.put_nowait, (flag_id, reason)
                )
            except Exception:
                logger.exception(
                    "Failed to enqueue kill-switch for flag %s.", flag_id
                )

        persistence_worker = FlagPersistenceWorker(
            dispatcher=dispatcher,
            get_db=deps.get_db,
            on_kill_switch=_on_kill_switch,
        )

        dispatcher.start()
        persistence_worker.start()

        # Start the heartbeat task.
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(
                websocket,
                interval=deps.heartbeat_interval_seconds,
                timeout=deps.heartbeat_timeout_seconds,
            )
        )

        # Spawn the kill-switch drain task.  It reads from
        # ``kill_switch_queue`` (populated by the persistence worker
        # via ``call_soon_threadsafe``) and sends the kill-switch
        # message over the WS using the existing ``DeliveryService``
        # for ``flag_id`` tracking.
        kill_switch_drain_task = _start_kill_switch_drain(
            websocket=websocket,
            delivery=delivery,
            kill_switch_queue=kill_switch_queue,
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
            # Cancel the kill-switch drain task first so it stops
            # trying to send on a closing WS.
            kill_switch_drain_task.cancel()
            try:
                await kill_switch_drain_task
            except asyncio.CancelledError:
                pass
            # Stop the dispatcher + persistence worker.  These run
            # in their own threads and have a bounded drain timeout
            # to prevent blocking shutdown.
            persistence_worker.stop(timeout=5.0)
            dispatcher.stop(timeout=5.0)
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

def _start_kill_switch_drain(
    *,
    websocket: WebSocket,
    delivery: DeliveryService,
    kill_switch_queue: asyncio.Queue,
) -> asyncio.Task:
    """Spawn a task that drains ``kill_switch_queue`` and sends the
    kill-switch messages over the WebSocket.

    Each queue item is a ``(flag_id, reason)`` tuple.  The drain
    task:

    1. Calls ``delivery.prepare_kill_switch(flag_id, reason)`` to
       build the kill-switch message and register it as pending.
    2. Sends the message via ``websocket.send_json(...)``.

    Returns the task handle so the caller can cancel it on disconnect.
    """

    async def _drain() -> None:
        while True:
            flag_id, reason = await kill_switch_queue.get()
            try:
                msg = delivery.prepare_kill_switch(flag_id, reason)
            except Exception:
                logger.exception(
                    "Failed to prepare kill-switch for flag %s.", flag_id,
                )
                continue
            try:
                await websocket.send_json(msg.to_json_dict())
                logger.info(
                    "Kill-switch sent for flag %s (reason=%s).",
                    flag_id, reason,
                )
            except Exception:
                logger.exception(
                    "Failed to send kill-switch for flag %s; "
                    "WS is likely closed.", flag_id,
                )

    return asyncio.create_task(_drain(), name="KillSwitchDrain")


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
