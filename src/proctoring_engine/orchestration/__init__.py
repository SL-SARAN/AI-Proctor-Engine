"""API / orchestration package.

Implements the FastAPI route surface, the session lifecycle state
machine, and the LTI-role-derived authorization model described in
``docs/07-api-orchestration-design.md``.

This layer's job is routing, authentication / authorization, and
state transitions triggered by decisions made elsewhere — it does
**not** make flagging or termination decisions.  Those live in the
fusion engine (:mod:`proctoring_engine.fusion`).  Keeping decision
logic and orchestration logic in separate layers is what makes the
fusion engine's rules testable in isolation, without needing a live
WebSocket connection or LTI context to exercise them.

Sub-modules:

- :mod:`~proctoring_engine.orchestration._settings` —
  ``OrchestrationSettings``, env loader.
- :mod:`~proctoring_engine.orchestration._state_machine` —
  ``can_transition``, ``assert_transition``, ``apply_transition``,
  ``InvalidSessionTransition``.
- :mod:`~proctoring_engine.orchestration._auth` —
  ``require_internal_terminate_token``, ``require_admin_role``,
  ``require_session_owner_or_admin``, ``parse_bearer``.
- :mod:`~proctoring_engine.orchestration._flag_persistence` —
  ``persist_flag_decision``, ``assert_flag_present``,
  ``FlagPersistenceError``.
- :mod:`~proctoring_engine.orchestration._admin_service` —
  ``create_policy_version``, ``create_exemption``,
  ``list_flags_for_session``, ``record_proctor_review``, plus
  the four typed errors.
- :mod:`~proctoring_engine.orchestration._evidence_service` —
  ``seal_evidence_for_flag``, plus the typed errors.
- :mod:`~proctoring_engine.orchestration._schemas` — Pydantic v2
  request / response models for the route surface.
- :mod:`~proctoring_engine.orchestration._routes` —
  ``build_orchestration_router(deps)``.
"""

from proctoring_engine.orchestration._admin_service import (
    AdminServiceError,
    ExemptionValidationError,
    FlagNotFoundError,
    PolicyVersioningError,
    PolicyVersionResult,
    ReviewResult,
    ReviewTransitionError,
    create_exemption,
    create_policy_version,
    list_exemptions,
    list_flags_for_session,
    list_policy_configs,
    record_proctor_review,
)
from proctoring_engine.orchestration._auth import (
    parse_bearer,
    require_admin_role,
    require_internal_terminate_token,
    require_session_owner_or_admin,
)
from proctoring_engine.orchestration._evidence_service import (
    EvidenceAlreadySealedError,
    EvidenceBlobTooLargeError,
    SealEvidenceServiceResult,
    seal_evidence_for_flag,
)
from proctoring_engine.orchestration._flag_persistence import (
    FlagPersistenceError,
    assert_flag_present,
    persist_flag_decision,
)
from proctoring_engine.orchestration._routes import (
    _OrchestrationDeps,
    build_orchestration_router,
)
from proctoring_engine.orchestration._settings import (
    OrchestrationSettings,
    get_orchestration_settings,
    reset_orchestration_settings,
    set_orchestration_settings,
)
from proctoring_engine.orchestration._state_machine import (
    InvalidSessionTransition,
    allowed_targets,
    apply_transition,
    assert_transition,
    can_transition,
)


__all__ = [
    # Settings
    "OrchestrationSettings",
    "get_orchestration_settings",
    "set_orchestration_settings",
    "reset_orchestration_settings",
    # State machine
    "InvalidSessionTransition",
    "can_transition",
    "assert_transition",
    "apply_transition",
    "allowed_targets",
    # Auth
    "parse_bearer",
    "require_internal_terminate_token",
    "require_admin_role",
    "require_session_owner_or_admin",
    # Flag persistence
    "FlagPersistenceError",
    "assert_flag_present",
    "persist_flag_decision",
    # Admin service
    "AdminServiceError",
    "ExemptionValidationError",
    "FlagNotFoundError",
    "PolicyVersioningError",
    "PolicyVersionResult",
    "ReviewResult",
    "ReviewTransitionError",
    "create_exemption",
    "create_policy_version",
    "list_exemptions",
    "list_flags_for_session",
    "list_policy_configs",
    "record_proctor_review",
    # Evidence service
    "EvidenceAlreadySealedError",
    "EvidenceBlobTooLargeError",
    "SealEvidenceServiceResult",
    "seal_evidence_for_flag",
    # Routes
    "_OrchestrationDeps",
    "build_orchestration_router",
]