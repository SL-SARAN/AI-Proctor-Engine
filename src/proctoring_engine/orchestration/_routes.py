"""FastAPI router for the API / orchestration layer.

Implements the routes from
:mod:`docs/07-api-orchestration-design.md` §1:

* ``GET  /sessions/{id}/status``  — student or admin reads the
  current session status (consent + lifecycle).
* ``POST /sessions/{id}/terminate``  — internal-only termination
  route.  Authorised by the
  :func:`proctoring_engine.orchestration._auth.require_internal_terminate_token`
  shared secret; rejects LTI session tokens (the locked invariant).
* ``POST /admin/policy-config``  — INSERT a new ``PolicyConfig``
  version row, optionally retire the previous active row in the
  same transaction.
* ``GET  /admin/policy-config``  — list all rows (admin view of
  the version history).
* ``POST /admin/accommodation-exemptions``  — INSERT a new
  accommodation exemption.
* ``GET  /admin/accommodation-exemptions``  — list exemptions,
  optionally filtered by participant or object class.
* ``GET  /admin/flags/{session_id}``  — review queue: list every
  ``Flag`` for a session, joined with ``EvidenceArtifact`` and
  ``ProctorReview`` rows.
* ``POST /admin/flags/{flag_id}/review``  — INSERT a
  ``ProctorReview`` row (append-only; never mutates the
  ``Flag`` row).

Plus one deferred-gap route from
:mod:`ARCHITECTURE.md` §7: ``POST
/sessions/{id}/flags/{flag_id}/evidence`` — the client-driven
evidence flush path that triggers ``seal_evidence_for_flag``.

The router is built via :func:`build_orchestration_router(deps)` so
tests can swap in their own ``get_db`` factory, settings, and
:class:`EvidenceStore` instance.  Every route returns the closed
error-code envelope on failure (see :data:`_ERROR_HTTP`).
"""

from __future__ import annotations

import logging
import uuid as _uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Header,
    HTTPException,
    Path,
    Query,
    UploadFile,
    status,
)
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from proctoring_engine.evidence._protocol import EvidenceStore
from proctoring_engine.evidence.service import SealEvidenceRequest
from proctoring_engine.lti.config import LtiSettings
from proctoring_engine.models import (
    ExamSession,
    SessionStatus,
    TerminationRecord,
)
from proctoring_engine.orchestration._admin_service import (
    ExemptionValidationError,
    FlagNotFoundError,
    PolicyVersioningError,
    ReviewTransitionError,
    assert_session_exists,
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
    seal_evidence_for_flag,
)
from proctoring_engine.orchestration._errors import (
    _ERROR_HTTP,
    http_error as _http_error,
)
from proctoring_engine.orchestration._flag_persistence import (
    FlagPersistenceError,
    assert_flag_present,
)
from proctoring_engine.orchestration._schemas import (
    AccommodationExemptionResponse,
    CreateExemptionRequest,
    CreatePolicyConfigRequest,
    CreateProctorReviewRequest,
    ErrorBody,
    FlagListResponse,
    FlagReviewResponse,
    PolicyConfigResponse,
    ProctorReviewCreatedResponse,
    SealEvidenceApiRequest,
    SealEvidenceApiResponse,
    SessionStatusResponse,
    TerminateRequest,
    TerminateResponse,
)
from proctoring_engine.orchestration._settings import OrchestrationSettings
from proctoring_engine.orchestration._state_machine import (
    InvalidSessionTransition,
    assert_transition,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed error-code → HTTP status mapping
# ---------------------------------------------------------------------------


#: The closed mapping from closed error code to HTTP status.  Codes
#: not in this map default to 400 (the conservative choice).  Adding
#: a code requires adding a row here, in the auth / services
#: modules, and in the integration test suite.
_ERROR_HTTP: dict[str, int] = {
    "internal_token_required": status.HTTP_401_UNAUTHORIZED,
    "internal_token_invalid": status.HTTP_403_FORBIDDEN,
    "session_token_required": status.HTTP_401_UNAUTHORIZED,
    "session_token_invalid": status.HTTP_401_UNAUTHORIZED,
    "session_token_expired": status.HTTP_401_UNAUTHORIZED,
    "not_authorized": status.HTTP_403_FORBIDDEN,
    "session_not_found": status.HTTP_404_NOT_FOUND,
    "flag_not_found": status.HTTP_404_NOT_FOUND,
    "policy_not_found": status.HTTP_404_NOT_FOUND,
    "policy_already_retired": status.HTTP_409_CONFLICT,
    "invalid_session_transition": status.HTTP_409_CONFLICT,
    "policy_versioning_error": 422,
    "exemption_validation_error": 422,
    "review_transition_error": status.HTTP_409_CONFLICT,
    "evidence_already_sealed": status.HTTP_409_CONFLICT,
    "evidence_blob_too_large": 413,
    "evidence_seal_failed": status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _http_error(code: str, detail: str = "") -> HTTPException:
    """Build an :class:`HTTPException` with the closed error-code envelope."""

    return HTTPException(
        status_code=_ERROR_HTTP.get(code, status.HTTP_400_BAD_REQUEST),
        detail=ErrorBody(code=code, message=detail or code).model_dump(),
    )


# ---------------------------------------------------------------------------
# Router dependencies (test-injectable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _OrchestrationDeps:
    """The dependencies :func:`build_orchestration_router` threads
    into the route handlers.

    Mirrors ``_RouterDeps`` from the LTI layer — the same pattern of
    explicit, test-injectable wiring.  A route handler that needs the
    ``EvidenceStore`` reads it from ``deps.evidence_store``, not
    from a hidden module-level global.
    """

    settings: OrchestrationSettings
    lti_settings: LtiSettings
    get_db: Callable[[], Session]
    evidence_store: EvidenceStore

    @property
    def default_retention_seconds(self) -> int:
        return self.settings.retention_default_seconds


# Dependency-callable closures — built once per router so the per-request
# FastAPI dependency-injection machinery can bind them at the right
# request scope.  Each one closes over ``deps`` and produces a callable
# matching FastAPI's ``Depends`` signature for that specific
# authentication / authorization decision.


def _make_internal_terminate_dependency(deps: _OrchestrationDeps):
    def _dep(authorization: str | None = Header(default=None)) -> None:
        require_internal_terminate_token(
            settings=deps.settings, authorization=authorization
        )

    return _dep


def _make_admin_role_dependency(deps: _OrchestrationDeps):
    def _dep(authorization: str | None = Header(default=None)):
        return require_admin_role(
            settings=deps.lti_settings,
            get_db=deps.get_db,
            authorization=authorization,
        )

    return _dep


def _make_session_owner_or_admin_dependency(deps: _OrchestrationDeps):
    def _dep(
        session_id: str = Path(..., min_length=1),
        authorization: str | None = Header(default=None),
    ):
        return require_session_owner_or_admin(
            settings=deps.lti_settings,
            get_db=deps.get_db,
            session_id=session_id,
            authorization=authorization,
        )

    return _dep


def build_orchestration_router(deps: _OrchestrationDeps) -> APIRouter:
    """Build the orchestration router.

    Returns a FastAPI :class:`APIRouter` with the routes from
    :mod:`docs/07-api-orchestration-design.md` §1 plus the
    client-driven evidence flush route.  The router is not wired to
    a database session globally; the session is requested via
    ``deps.get_db`` so the integration test can swap in a
    transactional session.
    """

    router = APIRouter(tags=["orchestration"])

    internal_terminate_dep = _make_internal_terminate_dependency(deps)
    admin_role_dep = _make_admin_role_dependency(deps)
    session_owner_dep = _make_session_owner_or_admin_dependency(deps)

    # ------------------------------------------------------------------
    # GET /sessions/{session_id}/status
    # ------------------------------------------------------------------

    @router.get(
        "/sessions/{session_id}/status",
        response_model=SessionStatusResponse,
    )
    def get_session_status(
        ctx: tuple[Any, ExamSession] = Depends(session_owner_dep),
    ) -> SessionStatusResponse:
        _claims, exam_session = ctx
        return SessionStatusResponse(
            session_id=exam_session.id,
            status=exam_session.status.value,
            consent_recorded=exam_session.consent_recorded_at is not None,
            started_at=exam_session.started_at,
            ended_at=exam_session.ended_at,
            retention_expires_at=exam_session.retention_expires_at,
            accumulated_medium_score=float(
                exam_session.accumulated_medium_score
            ),
        )

    # ------------------------------------------------------------------
    # POST /sessions/{session_id}/terminate  (internal)
    # ------------------------------------------------------------------

    @router.post(
        "/sessions/{session_id}/terminate",
        response_model=TerminateResponse,
    )
    def post_terminate(
        session_id: str = Path(..., min_length=1),
        request: TerminateRequest = Body(...),
        _internal: None = Depends(internal_terminate_dep),
    ) -> TerminateResponse:
        try:
            sid = _uuid.UUID(session_id)
        except ValueError as exc:
            raise _http_error(
                "session_not_found",
                f"session_id {session_id!r} is not a valid UUID",
            ) from exc
        flag_id = request.triggering_flag_id
        db = deps.get_db()
        try:
            session = db.get(ExamSession, sid)
            if session is None:
                raise _http_error(
                    "session_not_found",
                    f"no session with id {session_id!r}",
                )
            try:
                assert_flag_present(db, flag_id)
            except FlagPersistenceError as exc:
                raise _http_error("flag_not_found", str(exc)) from exc
            try:
                assert_transition(
                    session.status, SessionStatus.TERMINATED
                )
            except InvalidSessionTransition as exc:
                raise _http_error(
                    "invalid_session_transition", str(exc)
                ) from exc
            session.status = SessionStatus.TERMINATED
            session.ended_at = datetime.now(timezone.utc)
            termination = TerminationRecord(
                exam_session_id=session.id,
                triggering_flag_id=flag_id,
                reason=request.reason,
            )
            db.add(termination)
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise _http_error(
                "evidence_seal_failed",
                f"termination insert failed: {exc.orig}",
            ) from exc
        db.refresh(termination)
        return TerminateResponse(
            session_id=session.id,
            new_status=session.status.value,
            termination_record_id=termination.id,
            triggering_flag_id=flag_id,
        )

    # ------------------------------------------------------------------
    # /admin/policy-config
    # ------------------------------------------------------------------

    @router.post(
        "/admin/policy-config",
        response_model=PolicyConfigResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def post_policy_config(
        request: CreatePolicyConfigRequest = Body(...),
        admin: Any = Depends(admin_role_dep),
    ) -> PolicyConfigResponse:
        try:
            result = create_policy_version(
                deps.get_db(),
                request=request,
                created_by=admin,
                now=datetime.now(timezone.utc),
            )
        except PolicyVersioningError as exc:
            raise _http_error("policy_versioning_error", str(exc)) from exc
        return PolicyConfigResponse.from_orm(result.new_policy)

    @router.get(
        "/admin/policy-config",
        response_model=list[PolicyConfigResponse],
    )
    def get_policy_config(
        is_active: bool | None = Query(default=None),
        _admin: Any = Depends(admin_role_dep),
    ) -> list[PolicyConfigResponse]:
        rows = list_policy_configs(deps.get_db(), is_active=is_active)
        return [PolicyConfigResponse.from_orm(row) for row in rows]

    # ------------------------------------------------------------------
    # /admin/accommodation-exemptions
    # ------------------------------------------------------------------

    @router.post(
        "/admin/accommodation-exemptions",
        response_model=AccommodationExemptionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def post_exemption(
        request: CreateExemptionRequest = Body(...),
        admin: Any = Depends(admin_role_dep),
    ) -> AccommodationExemptionResponse:
        try:
            exemption = create_exemption(
                deps.get_db(),
                request=request,
                approver=admin,
                now=datetime.now(timezone.utc),
            )
        except ExemptionValidationError as exc:
            raise _http_error(
                "exemption_validation_error", str(exc)
            ) from exc
        return AccommodationExemptionResponse.from_orm(exemption)

    @router.get(
        "/admin/accommodation-exemptions",
        response_model=list[AccommodationExemptionResponse],
    )
    def get_exemptions(
        participant_id: _uuid.UUID | None = Query(default=None),
        object_class: str | None = Query(default=None, min_length=1),
        _admin: Any = Depends(admin_role_dep),
    ) -> list[AccommodationExemptionResponse]:
        rows = list_exemptions(
            deps.get_db(),
            participant_id=participant_id,
            object_class=object_class,
        )
        return [
            AccommodationExemptionResponse.from_orm(row) for row in rows
        ]

    # ------------------------------------------------------------------
    # /admin/flags/{session_id}
    # ------------------------------------------------------------------

    @router.get(
        "/admin/flags/{session_id}",
        response_model=FlagListResponse,
    )
    def get_flags(
        session_id: _uuid.UUID = Path(...),
        include_suppressed: bool = Query(default=True),
        _admin: Any = Depends(admin_role_dep),
    ) -> FlagListResponse:
        db = deps.get_db()
        try:
            assert_session_exists(db, session_id)
        except FlagNotFoundError as exc:
            raise _http_error("session_not_found", str(exc)) from exc
        flags, omitted = list_flags_for_session(
            db,
            session_id=session_id,
            include_suppressed=include_suppressed,
        )
        out: list[FlagReviewResponse] = []
        for flag in flags:
            row = FlagReviewResponse.from_orm(
                flag, include_suppressed=include_suppressed
            )
            if row is not None:
                out.append(row)
        return FlagListResponse(
            session_id=session_id,
            flags=out,
            omitted_suppressed_count=omitted,
        )

    # ------------------------------------------------------------------
    # /admin/flags/{flag_id}/review
    # ------------------------------------------------------------------

    @router.post(
        "/admin/flags/{flag_id}/review",
        response_model=ProctorReviewCreatedResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def post_review(
        flag_id: _uuid.UUID = Path(...),
        request: CreateProctorReviewRequest = Body(...),
        admin: Any = Depends(admin_role_dep),
    ) -> ProctorReviewCreatedResponse:
        try:
            result = record_proctor_review(
                deps.get_db(),
                flag_id=flag_id,
                decision_value=request.decision.value,
                notes=request.notes,
                reviewer=admin,
                now=datetime.now(timezone.utc),
            )
        except FlagNotFoundError as exc:
            raise _http_error("flag_not_found", str(exc)) from exc
        except ReviewTransitionError as exc:
            raise _http_error(
                "review_transition_error", str(exc)
            ) from exc
        return ProctorReviewCreatedResponse(
            id=result.review.id,
            flag_id=flag_id,
            decision=request.decision,
            session_status=result.session_status.value,
        )

    # ------------------------------------------------------------------
    # /sessions/{session_id}/flags/{flag_id}/evidence  (deferred §7 gap)
    # ------------------------------------------------------------------

    @router.post(
        "/sessions/{session_id}/flags/{flag_id}/evidence",
        response_model=SealEvidenceApiResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def post_seal_evidence(
        session_id: _uuid.UUID = Path(...),
        flag_id: _uuid.UUID = Path(...),
        artifact_type: str = Query(..., min_length=1),
        media_type: str = Query(..., min_length=1),
        capture_started_at: datetime = Query(...),
        capture_ended_at: datetime | None = Query(default=None),
        retention_expires_at: datetime | None = Query(default=None),
        blob: UploadFile = File(...),
    ) -> SealEvidenceApiResponse:
        # Validate the metadata via the Pydantic schema so the wire
        # shape is locked even if a client passes an unexpected
        # ``artifact_type`` literal.
        try:
            payload = SealEvidenceApiRequest(
                artifact_type=artifact_type,  # type: ignore[arg-type]
                media_type=media_type,
                capture_started_at=capture_started_at,
                capture_ended_at=capture_ended_at,
                retention_expires_at=retention_expires_at,
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "validation_error",
                    "message": (
                        exc.errors()[0]["msg"]
                        if exc.errors()
                        else "validation_error"
                    ),
                },
            ) from exc

        blob_bytes = await blob.read()
        db = deps.get_db()
        try:
            result = seal_evidence_for_flag(
                db,
                deps.evidence_store,
                request=SealEvidenceRequest(
                    flag_id=flag_id,
                    exam_session_id=session_id,
                    artifact_type=payload.artifact_type,
                    media_type=payload.media_type,
                    blob=blob_bytes,
                    capture_started_at=payload.capture_started_at,
                    capture_ended_at=payload.capture_ended_at,
                    retention_expires_at=payload.retention_expires_at
                    or datetime.now(timezone.utc),
                ),
                default_retention_seconds=deps.default_retention_seconds,
                now=datetime.now(timezone.utc),
            )
        except EvidenceAlreadySealedError as exc:
            raise _http_error("evidence_already_sealed", str(exc)) from exc
        except EvidenceBlobTooLargeError as exc:
            raise _http_error("evidence_blob_too_large", str(exc)) from exc

        return SealEvidenceApiResponse(
            evidence_artifact_id=result.artifact.id,
            flag_id=flag_id,
            storage_uri=result.artifact.storage_uri,
            content_sha256=result.artifact.content_sha256,
            byte_size=result.artifact.byte_size,
            media_type=result.artifact.media_type,
        )

    return router


# Re-export the small parser helper so tests can verify the
# Authorization-header shape directly.
__all__ = [
    "_ERROR_HTTP",
    "_OrchestrationDeps",
    "_http_error",
    "build_orchestration_router",
    "parse_bearer",
]