"""Environment-backed LTI / session-token settings.

Loaded at process start by :func:`get_lti_settings`; mutable per
process so tests can override individual fields without re-importing
the module. The dataclass is frozen, so override is by replacement
(:func:`set_lti_settings`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from os import getenv
from typing import Optional

from dotenv import load_dotenv


_DEFAULT_STATE_STORE_TTL_SECONDS = 600
_DEFAULT_SESSION_TOKEN_TTL_SECONDS = 14400
_DEFAULT_SESSION_TOKEN_ISSUER = "proctoring-engine"
_DEFAULT_SESSION_TOKEN_AUDIENCE = "proctoring-client"
_DEFAULT_EXAM_CLIENT_URL = "http://localhost:5173/exam"
_DEFAULT_ADMIN_SURFACE_URL = "http://localhost:5173/admin"
_DEFAULT_OIDC_HTTP_TIMEOUT_SECONDS = 5.0
_DEFAULT_STATE_STORE_BACKEND = "memory"
_VALID_STATE_STORE_BACKENDS = frozenset({"memory", "redis"})


@dataclass(frozen=True, slots=True)
class LtiSettings:
    """Configuration consumed by the LTI launch handler and the
    session-token issuer.

    The fields are the *minimum* the launch flow needs at runtime.
    Per-platform specifics (the set of accepted issuers, the JWKS
    cache size) belong in deployment configuration, not in the
    dataclass.
    """

    tool_client_id: str
    launch_url: str
    session_token_secret: str
    oidc_discovery_url: Optional[str] = None
    oidc_jwks_url: Optional[str] = None
    audience_override: Optional[str] = None
    state_store_ttl_seconds: int = _DEFAULT_STATE_STORE_TTL_SECONDS
    session_token_ttl_seconds: int = _DEFAULT_SESSION_TOKEN_TTL_SECONDS
    session_token_issuer: str = _DEFAULT_SESSION_TOKEN_ISSUER
    session_token_audience: str = _DEFAULT_SESSION_TOKEN_AUDIENCE
    # Reserved for v2 — deep linking launches. Captured here so the
    # dataclass shape does not have to change when v2 lands.
    deep_link_url: Optional[str] = None
    # The URL the launch handler 302s a learner to, with
    # ``?session_token=...``. The admin surface and the exam
    # client are separate processes in v1; both URLs are
    # environment-specific and HTTPS in production.
    exam_client_url: str = _DEFAULT_EXAM_CLIENT_URL
    admin_surface_url: str = _DEFAULT_ADMIN_SURFACE_URL
    # The timeout the production ``httpx.AsyncClient`` uses for
    # OIDC discovery and JWKS fetches. Tests override this with
    # a smaller value where the real timeout is a noisy bound.
    oidc_http_timeout_seconds: float = _DEFAULT_OIDC_HTTP_TIMEOUT_SECONDS
    # Which launch-state backend to wire in. ``"memory"`` (default)
    # is the single-replica fast path; ``"redis"`` is required when
    # the api tier scales beyond one replica, per
    # ``docs/DEPLOYMENT.md`` §6.2.
    state_store_backend: str = _DEFAULT_STATE_STORE_BACKEND
    # Redis connection URL, only consulted when
    # ``state_store_backend == "redis"``. Examples:
    # ``redis://redis:6379/0`` (no auth, in-cluster)
    # ``redis://:password@host:6379/0`` (with auth)
    redis_url: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate the secret length and numeric bounds.

        HS256 (per :mod:`proctoring_engine.lti.session_token`) requires
        a key of at least 32 bytes of entropy; rejecting shorter values
        at startup prevents a misconfigured deploy from silently issuing
        weak tokens.
        """

        if len(self.session_token_secret.encode("utf-8")) < 32:
            raise ValueError(
                "session_token_secret must be at least 32 bytes; "
                "generate with `secrets.token_urlsafe(32)` or equivalent"
            )
        if self.state_store_ttl_seconds <= 0:
            raise ValueError("state_store_ttl_seconds must be positive")
        if self.session_token_ttl_seconds <= 0:
            raise ValueError("session_token_ttl_seconds must be positive")
        if self.oidc_http_timeout_seconds <= 0:
            raise ValueError("oidc_http_timeout_seconds must be positive")
        if not self.tool_client_id:
            raise ValueError("tool_client_id must be set")
        if not self.launch_url:
            raise ValueError("launch_url must be set")
        if not self.exam_client_url:
            raise ValueError("exam_client_url must be set")
        if not self.admin_surface_url:
            raise ValueError("admin_surface_url must be set")
        if self.state_store_backend not in _VALID_STATE_STORE_BACKENDS:
            raise ValueError(
                f"state_store_backend must be one of "
                f"{sorted(_VALID_STATE_STORE_BACKENDS)}, "
                f"got {self.state_store_backend!r}"
            )
        if self.state_store_backend == "redis" and not self.redis_url:
            raise ValueError(
                "redis_url must be set when state_store_backend == 'redis'"
            )


_settings: LtiSettings | None = field(default=None, init=False)


def get_lti_settings() -> LtiSettings:
    """Return the process-local LTI settings, loading from env on first
    access.
    """

    global _settings
    if _settings is None:
        load_dotenv(override=False)
        _settings = _load_from_env()
    return _settings


def set_lti_settings(settings: LtiSettings) -> None:
    """Replace the process-local settings.

    Intended for tests and for the FastAPI startup hook when settings
    come from a secrets manager rather than the environment.
    """

    global _settings
    _settings = settings


def reset_lti_settings() -> None:
    """Clear the process-local settings.

    The next :func:`get_lti_settings` call re-reads the environment.
    """

    global _settings
    _settings = None


def _load_from_env() -> LtiSettings:
    """Build an :class:`LtiSettings` from the current environment.

    Missing required variables produce a clear :class:`ValueError`
    that names the variable, not a generic config error.
    """

    tool_client_id = getenv("LTI_TOOL_CLIENT_ID", "")
    launch_url = getenv("LTI_LAUNCH_URL", "")
    secret = getenv("SESSION_TOKEN_SECRET", "")
    if not tool_client_id:
        raise ValueError("LTI_TOOL_CLIENT_ID is not set")
    if not launch_url:
        raise ValueError("LTI_LAUNCH_URL is not set")
    if not secret:
        raise ValueError("SESSION_TOKEN_SECRET is not set")
    return LtiSettings(
        tool_client_id=tool_client_id,
        launch_url=launch_url,
        session_token_secret=secret,
        oidc_discovery_url=_optional("LTI_OIDC_DISCOVERY_URL"),
        oidc_jwks_url=_optional("LTI_OIDC_JWKS_URL"),
        audience_override=_optional("LTI_AUDIENCE_OVERRIDE"),
        state_store_ttl_seconds=_int(
            "LTI_STATE_STORE_TTL_SECONDS",
            _DEFAULT_STATE_STORE_TTL_SECONDS,
        ),
        session_token_ttl_seconds=_int(
            "SESSION_TOKEN_TTL_SECONDS",
            _DEFAULT_SESSION_TOKEN_TTL_SECONDS,
        ),
        session_token_issuer=getenv(
            "SESSION_TOKEN_ISSUER",
            _DEFAULT_SESSION_TOKEN_ISSUER,
        ),
        session_token_audience=getenv(
            "SESSION_TOKEN_AUDIENCE",
            _DEFAULT_SESSION_TOKEN_AUDIENCE,
        ),
        exam_client_url=getenv("EXAM_CLIENT_URL", _DEFAULT_EXAM_CLIENT_URL),
        admin_surface_url=getenv("ADMIN_SURFACE_URL", _DEFAULT_ADMIN_SURFACE_URL),
        oidc_http_timeout_seconds=_float(
            "OIDC_HTTP_TIMEOUT_SECONDS",
            _DEFAULT_OIDC_HTTP_TIMEOUT_SECONDS,
        ),
        state_store_backend=getenv(
            "LTI_STATE_STORE_BACKEND",
            _DEFAULT_STATE_STORE_BACKEND,
        ),
        redis_url=_optional("REDIS_URL"),
    )


def _optional(name: str) -> Optional[str]:
    """Return an env var as ``None`` when empty, otherwise its value."""

    value = getenv(name)
    return value if value else None


def _int(name: str, default: int) -> int:
    """Return an env var as an ``int``, with a clear error on garbage."""

    raw = getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    """Return an env var as a ``float``, with a clear error on garbage."""

    raw = getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a float, got {raw!r}") from exc
