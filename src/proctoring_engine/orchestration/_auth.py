"""Authentication / authorization dependencies for the orchestration layer.

Three FastAPI dependencies, each a pure function over
``Request`` / ``Header`` / ``Session``:

* :func:`require_internal_terminate_token` — the **internal service
  credential** for ``POST /sessions/{id}/terminate``.  Distinct from
  any LTI-derived token per :mod:`docs/07-api-orchestration-design.md`
  §3 (the single internal exception to the LTI-roles authorization
  model).
* :func:`require_admin_role` — the **LTI session-token** credential
  for the four ``/admin/*`` routes.  Decodes the session token,
  requires ``role in {ADMIN, INSTRUCTOR, PROCTOR}``, and returns the
  matching :class:`AdminUser` row.
* :func:`require_session_owner_or_admin` — the **LTI session-token**
  credential for ``GET /sessions/{id}/status``.  Decodes the session
  token, requires the ``sub`` claim to equal the
  ``ExamSession.participant_id`` *or* the role to be admin.

All three reject with a closed error-code envelope (see
:mod:`proctoring_engine.orchestration._routes`).  The 401/403 split
is intentional and matches the LTI layer:

* **401** = "we don't know who you are" (no / malformed / expired
  credential).
* **403** = "we know, and you can't" (credential valid but
  authorization missing).

The internal terminate token uses
:func:`hmac.compare_digest` for constant-time comparison, so a
timing-attack probe of the route cannot learn anything about the
secret.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import Header, Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from proctoring_engine.lti.config import LtiSettings
from proctoring_engine.lti.roles import AppRole
from proctoring_engine.lti.session_token import (
    SessionClaims,
    SessionTokenError,
    SessionTokenExpired,
    SessionTokenInvalid,
    decode_session_token,
)
from proctoring_engine.models import AdminRole, AdminUser, ExamSession
from proctoring_engine.orchestration._errors import http_error as _http_error
from proctoring_engine.orchestration._settings import OrchestrationSettings


# ---------------------------------------------------------------------------
# Closed error-code envelope
# ---------------------------------------------------------------------------

# The closed mapping from error code → HTTP status lives in
# :mod:`proctoring_engine.orchestration._errors` so the auth and route
# layers can't drift.  Re-aliased here as ``_http_error`` so the
# helpers below read consistently.


# ---------------------------------------------------------------------------
# Internal terminate credential
# ---------------------------------------------------------------------------

#: Per RFC 6750 §2.1, the only Authorization scheme the internal route
#: accepts.  Anything else (Basic, Digest, custom schemes, bare
#: non-Bearer strings) is rejected with ``internal_token_required``.
_BEARER_SCHEME: str = "Bearer"


def parse_bearer(authorization: str | None) -> str | None:
    """Parse ``Authorization: Bearer <token>`` and return the token, or
    ``None`` if the header is missing / malformed.

    Exposed for tests.  The parser is strict: an empty bearer segment
    is treated as malformed (returns ``None``).
    """

    if authorization is None:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2:
        return None
    scheme, token = parts[0], parts[1].strip()
    if scheme.lower() != _BEARER_SCHEME.lower():
        return None
    if not token:
        return None
    return token


def require_internal_terminate_token(
    *,
    settings: OrchestrationSettings,
    authorization: str | None = Header(default=None),
) -> None:
    """Reject the request unless the bearer token equals the internal
    service credential.

    A student or instructor LTI session token (a JWT) will *not* match
    the configured shared secret, so the whole-string equality check
    alone is sufficient to discriminate — but the bearer parsing
    rejects malformed Authorization headers before the comparison
    happens, so the route gives a clear ``internal_token_required``
    on a missing / non-Bearer header rather than a confusing
    ``internal_token_invalid``.

    Uses :func:`hmac.compare_digest` for constant-time comparison so a
    timing-attack probe of the route cannot learn the secret by
    measuring response latency.
    """

    token = parse_bearer(authorization)
    if token is None:
        raise _http_error(
            "internal_token_required",
            "Authorization: Bearer <INTERNAL_TERMINATE_TOKEN> is required",
        )
    if not hmac.compare_digest(
        token.encode("utf-8"),
        settings.internal_terminate_token.encode("utf-8"),
    ):
        raise _http_error(
            "internal_token_invalid",
            "the supplied bearer token does not match the internal service credential",
        )


# ---------------------------------------------------------------------------
# Admin role (LTI session token)
# ---------------------------------------------------------------------------

#: The set of LTI roles that may access ``/admin/*``.  Mirrors
#: :func:`proctoring_engine.lti.roles.is_admin_route` but is duplicated
#: here so the orchestration layer doesn't import the LTI routing helper
#: for what is, semantically, an authorization decision.
_ADMIN_ROLES: frozenset[AppRole] = frozenset(
    {AppRole.ADMIN, AppRole.INSTRUCTOR, AppRole.PROCTOR}
)

#: Map from :class:`AppRole` to the persisted :class:`AdminRole` enum
#: on the ``AdminUser`` row.  Mirrors
#: :func:`proctoring_engine.lti.service._admin_role_for`.
_APP_ROLE_TO_ADMIN_ROLE: dict[AppRole, AdminRole] = {
    AppRole.ADMIN: AdminRole.ADMIN,
    AppRole.INSTRUCTOR: AdminRole.INSTRUCTOR,
    AppRole.PROCTOR: AdminRole.PROCTOR,
}


def _decode_or_raise(
    authorization: str | None,
    *,
    settings: LtiSettings,
) -> SessionClaims:
    """Parse bearer → session token → :class:`SessionClaims`.

    Raises the closed error-code envelope on every failure path.
    """

    if authorization is None:
        raise _http_error(
            "session_token_required",
            "Authorization: Bearer <session_token> is required",
        )
    token = parse_bearer(authorization)
    if token is None:
        raise _http_error(
            "session_token_required",
            "Authorization header must be a Bearer scheme",
        )
    try:
        return decode_session_token(token, settings=settings)
    except SessionTokenExpired as exc:
        raise _http_error(
            "session_token_expired", "the session token has expired"
        ) from exc
    except SessionTokenInvalid as exc:
        raise _http_error(
            "session_token_invalid",
            f"the session token is invalid: {exc}",
        ) from exc
    except SessionTokenError as exc:
        raise _http_error(
            "session_token_invalid",
            f"the session token could not be decoded: {exc}",
        ) from exc


def _load_admin_user(
    db: Session, claims: SessionClaims
) -> AdminUser:
    """Resolve the session-token subject to an :class:`AdminUser` row.

    The session token's ``sub`` claim carries the
    :class:`Participant.id` (a UUID), not the LMS subject string —
    that's how :mod:`proctoring_engine.lti.service.process_launch`
    issues the token.  The :class:`AdminUser` table is keyed by
    ``(lti_issuer, lms_user_reference)``, the same natural key
    :class:`Participant` uses.  To bridge the two, the lookup joins
    through the participant row:

    ``claims.subject`` (a UUID string) →
    :class:`Participant.id` → :class:`Participant.lms_user_reference` →
    :class:`AdminUser.lms_user_reference`.

    The :func:`proctoring_engine.lti.service.process_launch` upsert
    keeps ``Participant.lms_user_reference`` and
    ``AdminUser.lms_user_reference`` aligned on every launch, so the
    two-step lookup is safe.

    Returns the matching :class:`AdminUser` row.  Raises 403 with
    ``not_authorized`` when the participant row doesn't exist
    (an unauthenticated claim) or when no admin row exists for that
    participant (a learner-role claim would have been rejected by
    the caller before this point).
    """

    # Resolve the participant id (claims.subject) to a real row so
    # we can read its lms_user_reference.  ``participant.lms_user_reference``
    # is the LMS subject string that AdminUser is keyed on.
    from proctoring_engine.models import Participant

    try:
        participant_uuid = uuid.UUID(claims.subject)
    except (ValueError, AttributeError):
        raise _http_error(
            "not_authorized",
            "the session-token subject is not a valid participant id",
        ) from None

    participant = db.get(Participant, participant_uuid)
    if participant is None:
        raise _http_error(
            "not_authorized",
            "no participant is associated with the session-token subject",
        )

    admin = db.execute(
        select(AdminUser).where(
            AdminUser.lti_issuer == participant.lti_issuer,
            AdminUser.lms_user_reference == participant.lms_user_reference,
        )
    ).scalar_one_or_none()
    if admin is None:
        raise _http_error(
            "not_authorized",
            "no admin user is associated with the session-token subject",
        )
    return admin


def require_admin_role(
    *,
    settings: LtiSettings,
    get_db: Callable[[], Session],
    authorization: str | None = Header(default=None),
) -> AdminUser:
    """Decode the session token, require an admin / proctor / instructor
    role, and return the :class:`AdminUser` row.

    The role is the **persisted** :class:`AdminRole` on the row, not
    the in-flight :class:`AppRole` claim — promotions in the AdminUser
    table take effect immediately, demotions require a fresh launch
    (per :mod:`proctoring_engine.lti.service._admin_role_privilege`).
    """

    claims = _decode_or_raise(authorization, settings=settings)
    if claims.role not in _ADMIN_ROLES:
        raise _http_error(
            "not_authorized",
            "this route requires an admin, proctor, or instructor role",
        )
    db = get_db()
    return _load_admin_user(db, claims)


# ---------------------------------------------------------------------------
# Session owner or admin (LTI session token)
# ---------------------------------------------------------------------------


def require_session_owner_or_admin(
    *,
    settings: LtiSettings,
    get_db: Callable[[], Session],
    session_id: str = Path(..., min_length=1),
    authorization: str | None = Header(default=None),
) -> tuple[SessionClaims, ExamSession]:
    """Decode the session token, look up the session, and authorize
    either the participant owner (sub == participant_id) or any
    admin / proctor / instructor role.

    Returns the verified :class:`SessionClaims` and the loaded
    :class:`ExamSession` row.  Raises the closed error-code envelope
    on every failure path.  No information leakage: a request with a
    non-learner token that's not admin gets 403, the same as a request
    with a learner token whose ``sub`` doesn't match.
    """

    claims = _decode_or_raise(authorization, settings=settings)
    db = get_db()
    try:
        session = db.get(ExamSession, _parse_uuid(session_id))
    except ValueError as exc:
        raise _http_error(
            "session_not_found",
            f"session_id {session_id!r} is not a valid UUID",
        ) from exc
    if session is None:
        raise _http_error(
            "session_not_found", f"no session with id {session_id!r}"
        )

    if claims.role in _ADMIN_ROLES:
        return claims, session

    if claims.role == AppRole.LEARNER:
        if str(session.participant_id) == claims.subject:
            return claims, session

    raise _http_error(
        "not_authorized",
        "the session token does not authorize this session",
    )


def _parse_uuid(value: str) -> Any:
    """Parse a path-parameter UUID; raise :class:`ValueError` on garbage."""

    import uuid as _uuid

    return _uuid.UUID(value)


__all__ = [
    "parse_bearer",
    "require_admin_role",
    "require_internal_terminate_token",
    "require_session_owner_or_admin",
]
