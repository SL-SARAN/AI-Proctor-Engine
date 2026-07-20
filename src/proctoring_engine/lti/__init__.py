"""LTI 1.3 ingestion package.

Converts an LTI third-party-initiated login into a
:class:`proctoring_engine.models.ExamSession` row and issues a
short-lived signed session token for the WebSocket layer (next atomic
layer). See ``docs/02-ingestion-layer-design.md`` §1.

The public surface of this package is re-exported below so the
FastAPI app and the test suite can import everything from
``proctoring_engine.lti`` without reaching into the individual
modules.
"""

from proctoring_engine.lti.claims import (
    LtiClaimsError,
    LtiContext,
    LtiCustomClaims,
    LtiIdToken,
    LtiResourceLink,
    LtiToolPlatform,
    combined_context_id,
    require_policy_config_name,
)
from proctoring_engine.lti.config import (
    LtiSettings,
    get_lti_settings,
    reset_lti_settings,
    set_lti_settings,
)
from proctoring_engine.lti.discovery import (
    OidcDiscovery,
    OidcDiscoveryCache,
    OidcDiscoveryError,
)
from proctoring_engine.lti.jwks import JwksCache, JwksError
from proctoring_engine.lti.roles import AppRole, is_admin_route, map_roles
from proctoring_engine.lti.routes import build_lti_router
from proctoring_engine.lti.service import (
    LaunchResult,
    LtiLaunchError,
    LtiLaunchErrorCode,
    process_launch,
)
from proctoring_engine.lti.session_token import (
    SessionClaims,
    SessionTokenError,
    SessionTokenExpired,
    SessionTokenInvalid,
    decode_session_token,
    issue_session_token,
)
from proctoring_engine.lti.state import (
    LaunchStateError,
    LaunchStateExpired,
    LaunchStateMissing,
    LaunchStateStore,
)


__all__ = [
    "AppRole",
    "JwksCache",
    "JwksError",
    "LaunchResult",
    "LaunchStateError",
    "LaunchStateExpired",
    "LaunchStateMissing",
    "LaunchStateStore",
    "LtiClaimsError",
    "LtiContext",
    "LtiCustomClaims",
    "LtiIdToken",
    "LtiLaunchError",
    "LtiLaunchErrorCode",
    "LtiResourceLink",
    "LtiSettings",
    "LtiToolPlatform",
    "OidcDiscovery",
    "OidcDiscoveryCache",
    "OidcDiscoveryError",
    "SessionClaims",
    "SessionTokenError",
    "SessionTokenExpired",
    "SessionTokenInvalid",
    "build_lti_router",
    "combined_context_id",
    "decode_session_token",
    "get_lti_settings",
    "is_admin_route",
    "issue_session_token",
    "map_roles",
    "process_launch",
    "require_policy_config_name",
    "reset_lti_settings",
    "set_lti_settings",
]
