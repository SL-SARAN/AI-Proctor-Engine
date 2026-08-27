"""Closed error-code envelope shared between the auth layer and the
routes layer.

The orchestration layer raises :class:`fastapi.HTTPException` from
two places — the auth dependencies in
:mod:`proctoring_engine.orchestration._auth`, and the route handlers
in :mod:`proctoring_engine.orchestration._routes`.  Both layers must
map the same set of closed error codes to the same HTTP statuses, so
the mapping lives here and is imported by both.

Adding a new error code is a three-step change: add the code to
:data:`_ERROR_HTTP` below, raise it from the appropriate auth /
service / route site, and add an integration test that asserts on
the closed envelope shape (``{"detail": {"code": ..., "message": ...}}``).
"""

from __future__ import annotations

from fastapi import HTTPException


#: The closed mapping from closed error code to HTTP status.  Codes
#: not in this map default to 400 (the conservative choice).  Adding
#: a code requires adding a row here, in the auth / services
#: modules, and in the integration test suite.
_ERROR_HTTP: dict[str, int] = {
    "internal_token_required": 401,
    "internal_token_invalid": 403,
    "session_token_required": 401,
    "session_token_invalid": 401,
    "session_token_expired": 401,
    "not_authorized": 403,
    "session_not_found": 404,
    "flag_not_found": 404,
    "policy_not_found": 404,
    "policy_already_retired": 409,
    "invalid_session_transition": 409,
    "policy_versioning_error": 422,
    "exemption_validation_error": 422,
    "review_transition_error": 409,
    "evidence_already_sealed": 409,
    "evidence_blob_too_large": 413,
    "evidence_seal_failed": 500,
    "identity_override_not_found": 404,
    "admin_user_not_found": 404,
    "self_approval_rejected": 403,
    "role_unauthorized": 403,
}


def http_error(code: str, detail: str = "") -> HTTPException:
    """Build an :class:`HTTPException` carrying the closed envelope.

    Uses the canonical mapping in :data:`_ERROR_HTTP`.  Codes not in
    the map default to 400.  This is the **only** helper that should
    raise the closed error envelope in the orchestration layer; both
    the auth dependencies and the route handlers call it.
    """

    status_code = _ERROR_HTTP.get(code, 400)
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": detail or code},
    )


__all__ = ["_ERROR_HTTP", "http_error"]