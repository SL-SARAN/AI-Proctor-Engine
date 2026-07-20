"""Session-token issuer and decoder for the WebSocket layer.

The session token is a HS256-signed JWT carrying the participant
identity, the exam session identity, and the resolved application
role. The token is the single credential the WebSocket handshake
authenticates with (the next atomic layer).

The token is short-lived (default 4 hours, per the
``SESSION_TOKEN_TTL_SECONDS`` setting). A leaked token is
constrained to the exam window — long enough to be useful for a
test, short enough that a token captured mid-exam does not outlive
the exam itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt

from proctoring_engine.lti.config import LtiSettings, get_lti_settings
from proctoring_engine.lti.roles import AppRole


class SessionTokenError(Exception):
    """Base class for the session-token error surface."""


class SessionTokenInvalid(SessionTokenError):
    """Raised when a token is structurally invalid or fails
    cryptographic validation.
    """


class SessionTokenExpired(SessionTokenError):
    """Raised when a token is valid but its ``exp`` claim is in the
    past.
    """


@dataclass(frozen=True, slots=True)
class SessionClaims:
    """The decoded claims from a session token.

    The shape is the source of truth for the WebSocket-layer contract
    (next atomic layer). Adding a claim here is a contract change
    that the WS layer must consume; removing one is a breaking
    change for any already-issued token.
    """

    subject: str
    session_id: str
    role: AppRole
    issuer: str
    audience: str
    issued_at: datetime
    expires_at: datetime
    jti: str


def issue_session_token(
    participant_id: uuid.UUID,
    exam_session_id: uuid.UUID,
    role: AppRole,
    *,
    settings: Optional[LtiSettings] = None,
    now: Optional[datetime] = None,
) -> str:
    """Issue a signed session token for the given participant and
    exam session.

    The token carries the four application-level claims the WebSocket
    layer needs (``sub``, ``sid``, ``role``) plus the standard
    registered claims (``iss``, ``aud``, ``iat``, ``exp``, ``jti``).
    """

    cfg = settings or get_lti_settings()
    issued_at = now or datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=cfg.session_token_ttl_seconds)
    payload = {
        "sub": str(participant_id),
        "sid": str(exam_session_id),
        "role": role.value,
        "iss": cfg.session_token_issuer,
        "aud": cfg.session_token_audience,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, cfg.session_token_secret, algorithm="HS256")


def decode_session_token(
    token: str,
    *,
    settings: Optional[LtiSettings] = None,
) -> SessionClaims:
    """Validate and decode a session token.

    The decoder enforces the same ``iss``/``aud`` constraints the
    issuer used, and rejects expired tokens with a distinct
    exception type so the WebSocket layer can branch on the cause
    (an expired token is a soft "re-authenticate" event, not a
    hard authentication failure).

    Raises:
        SessionTokenInvalid: The token is structurally invalid, the
            signature does not verify, the ``iss`` or ``aud`` claim
            does not match, or a required claim is missing.
        SessionTokenExpired: The token verified but its ``exp``
            claim is in the past.
    """

    cfg = settings or get_lti_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            cfg.session_token_secret,
            algorithms=["HS256"],
            audience=cfg.session_token_audience,
            issuer=cfg.session_token_issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "sid", "role", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise SessionTokenExpired("session token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise SessionTokenInvalid(f"session token is invalid: {exc}") from exc

    return _claims_from_payload(payload)


def _claims_from_payload(payload: dict[str, Any]) -> SessionClaims:
    """Build a :class:`SessionClaims` from a verified JWT payload.

    Raises :class:`SessionTokenInvalid` on a missing or malformed
    claim. The standard claims (``iss``, ``aud``, ``iat``, ``exp``,
    ``jti``) are guaranteed by :func:`jwt.decode` when
    ``options.require`` lists them; this function checks the
    application-level claims (``sub``, ``sid``, ``role``).
    """

    try:
        subject = str(payload["sub"])
        session_id = str(payload["sid"])
        role_value = str(payload["role"])
        role = AppRole(role_value)
    except (KeyError, ValueError) as exc:
        raise SessionTokenInvalid(
            f"session token is missing or has malformed claims: {exc}"
        ) from exc

    try:
        issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=timezone.utc)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
    except (KeyError, ValueError, OSError) as exc:
        raise SessionTokenInvalid(
            f"session token iat/exp are not valid unix timestamps: {exc}"
        ) from exc

    return SessionClaims(
        subject=subject,
        session_id=session_id,
        role=role,
        issuer=str(payload["iss"]),
        audience=str(payload["aud"]),
        issued_at=issued_at,
        expires_at=expires_at,
        jti=str(payload["jti"]),
    )
