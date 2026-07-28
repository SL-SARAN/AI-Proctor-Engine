"""Environment-backed orchestration settings.

The API / orchestration layer (``docs/07-api-orchestration-design.md``)
needs one settings surface that the v1 production wiring depends on:

* ``INTERNAL_TERMINATE_TOKEN`` — the shared-secret credential the fusion
  engine uses to call ``POST /sessions/{id}/terminate``.  Distinct from
  any LTI-derived token per the design doc §3 (the orchestration layer's
  single internal exception to the LTI-roles authorization rule).

The dataclass is frozen + slotted, mirrors
:mod:`proctoring_engine.lti.config`, and exposes the same
``get_*_settings`` / ``set_*_settings`` / ``reset_*_settings`` pattern
the other layers use.  Override is by replacement; the dataclass
single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from os import getenv

from dotenv import load_dotenv


#: Default retention horizon in seconds (90 days).  Calibration note in
#: :mod:`proctoring_engine.evidence._settings`; the orchestration layer
#: stamps the value onto a freshly-sealed ``EvidenceArtifact`` when the
#: client doesn't supply one explicitly.  Configurable per deployment
#: via the ``ORCHESTRATION_RETENTION_DEFAULT_SECONDS`` env var.
_DEFAULT_RETENTION_SECONDS = 90 * 24 * 3600
#: ``_MIN_TOKEN_BYTES`` is the minimum length the bearer-credential
#: parser enforces at startup.  Mirrors the
#: ``SESSION_TOKEN_SECRET`` minimum in :mod:`proctoring_engine.lti.config`
#: so a misconfigured deploy fails loud rather than issuing a weak
#: shared secret.
_MIN_TOKEN_BYTES = 32


@dataclass(frozen=True, slots=True)
class OrchestrationSettings:
    """Resolved orchestration-layer settings.

    Attributes
    ----------
    internal_terminate_token:
        Shared secret the fusion engine attaches as
        ``Authorization: Bearer <token>`` to the internal
        ``POST /sessions/{id}/terminate`` route.  Distinct from any
        LTI-derived session token; never bound to a participant or
        admin identity (per the design doc §3 invariant).
    retention_default_seconds:
        Retention horizon stamped onto ``EvidenceArtifact.retention_expires_at``
        when the seal request didn't supply one explicitly.  Conservative
        default; the per-deployment value lives in
        ``ORCHESTRATION_RETENTION_DEFAULT_SECONDS``.
    """

    internal_terminate_token: str
    retention_default_seconds: int = _DEFAULT_RETENTION_SECONDS

    def __post_init__(self) -> None:
        if (
            len(self.internal_terminate_token.encode("utf-8"))
            < _MIN_TOKEN_BYTES
        ):
            raise ValueError(
                "INTERNAL_TERMINATE_TOKEN must be at least "
                f"{_MIN_TOKEN_BYTES} bytes; generate with "
                "`secrets.token_urlsafe(32)` or equivalent"
            )
        if self.retention_default_seconds <= 0:
            raise ValueError(
                "retention_default_seconds must be a positive integer"
            )


#: Process-local cache populated by :func:`get_orchestration_settings`
#: on first access and replaced by :func:`set_orchestration_settings`
#: in tests.
_settings: OrchestrationSettings | None = field(default=None, init=False)


def get_orchestration_settings() -> OrchestrationSettings:
    """Return the process-local orchestration settings, loading from
    env on first access.

    Raises
    ------
    ValueError:
        A required environment variable is missing or malformed.
    """

    global _settings
    if _settings is None:
        load_dotenv(override=False)
        _settings = _load_from_env()
    return _settings


def set_orchestration_settings(settings: OrchestrationSettings) -> None:
    """Replace the process-local cache.

    Intended for tests and for production wiring when the credentials
    come from a secrets manager rather than the environment.
    """

    global _settings
    _settings = settings


def reset_orchestration_settings() -> None:
    """Clear the process-local cache.

    The next :func:`get_orchestration_settings` call re-reads the
    environment.
    """

    global _settings
    _settings = None


def _load_from_env() -> OrchestrationSettings:
    """Build :class:`OrchestrationSettings` from the current environment.

    A missing ``INTERNAL_TERMINATE_TOKEN`` raises :class:`ValueError`
    naming the variable; the application lifespan catches the error
    and serves the v1 dev-mode shape (no orchestration routes,
    only ``/healthz``).
    """

    token = getenv("INTERNAL_TERMINATE_TOKEN", "")
    if not token:
        raise ValueError("INTERNAL_TERMINATE_TOKEN is not set")
    return OrchestrationSettings(
        internal_terminate_token=token,
        retention_default_seconds=_int(
            "ORCHESTRATION_RETENTION_DEFAULT_SECONDS",
            _DEFAULT_RETENTION_SECONDS,
        ),
    )


def _int(name: str, default: int) -> int:
    """Return an env var as an ``int`` with a clear error on garbage."""

    raw = getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


__all__ = [
    "OrchestrationSettings",
    "get_orchestration_settings",
    "set_orchestration_settings",
    "reset_orchestration_settings",
]
