"""Pydantic v2 request / response models for the orchestration route surface.

The orchestration layer's wire shape is intentionally minimal — the
state machine and authorization checks do most of the validation work;
these models are the serialization boundary that the FastAPI
endpoints hand to and receive from the client.

Each model carries:

* ``model_config`` — ``frozen=True`` to enforce value-equality
  semantics; ``extra="forbid"`` so unknown fields are rejected at
  parse time, not silently dropped (a closed wire shape is what makes
  the closed error-code envelope testable).
* A ``to_orm_kwargs`` / ``from_orm`` helper for the
  request → ORM boundary.  These helpers exist because Pydantic v2
  is the parsing layer; SQLAlchemy is the persistence layer.  Mixing
  the two directly (e.g. via ``model_validate(orm_instance)``) would
  smuggle ORM internal state into the wire format.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from proctoring_engine.models import (
    EvidenceKind,
    FlagSeverity,
    ReferenceMaterialPolicy,
    ReviewDecision,
)


# Closed pydantic config — every model in this module inherits it.
_CLOSED_CONFIG = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Shared response envelope
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    """The closed error-code envelope every orchestration route returns on
    failure.

    Closed (not free-form) so the integration test suite can assert on
    the code in the response body.  The :class:`code` values are
    defined in :mod:`proctoring_engine.orchestration._routes`.
    """

    model_config = _CLOSED_CONFIG

    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1)


class ErrorResponse(BaseModel):
    """HTTP-level wrapper for :class:`ErrorBody`."""

    model_config = _CLOSED_CONFIG

    detail: ErrorBody


# ---------------------------------------------------------------------------
# /sessions/{id}/status
# ---------------------------------------------------------------------------


class SessionStatusResponse(BaseModel):
    """The shape of ``GET /sessions/{id}/status``.

    ``consent_recorded`` is a derived boolean (``consent_recorded_at
    is not None``) the client can read to know whether the
    proctored session has been consented-to.  The route returns 200
    pre-consent — the field exists precisely so a status-querying
    client can tell that telemetry persistence is gated.
    """

    model_config = _CLOSED_CONFIG

    session_id: uuid.UUID
    status: str = Field(min_length=1)
    consent_recorded: bool
    started_at: datetime | None
    ended_at: datetime | None
    retention_expires_at: datetime | None
    accumulated_medium_score: float = Field(ge=0.0)


# ---------------------------------------------------------------------------
# /sessions/{id}/terminate  (internal)
# ---------------------------------------------------------------------------


class TerminateRequest(BaseModel):
    """The body the fusion engine sends to the internal terminate route.

    ``triggering_flag_id`` is **required** — every auto-termination
    is the consequence of a specific :class:`Flag` row, and the
    ``TerminationRecord.triggering_flag_id`` FK enforces this at
    the schema level.
    """

    model_config = _CLOSED_CONFIG

    triggering_flag_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=512)


class TerminateResponse(BaseModel):
    """The 200 response from the internal terminate route."""

    model_config = _CLOSED_CONFIG

    session_id: uuid.UUID
    new_status: str = Field(min_length=1)
    termination_record_id: uuid.UUID
    triggering_flag_id: uuid.UUID


# ---------------------------------------------------------------------------
# /admin/policy-config
# ---------------------------------------------------------------------------


class CreatePolicyConfigRequest(BaseModel):
    """The body the admin sends to ``POST /admin/policy-config``.

    The v1 design is: every POST creates a new version row.  The
    natural key ``name + is_active=True + retired_at IS NULL`` selects
    the *currently active* version; a POST with the same ``name`` as
    an existing active row should pass ``retire_previous=True`` so the
    old row is marked retired in the same transaction.

    The numeric fields mirror the ``PolicyConfig`` ORM defaults so a
    POST that omits them produces a row with the same thresholds as
    the spec defaults (``docs/proctoring-engine-v1-spec.md`` §3.1).
    ``is_active`` defaults to ``True``; ``retire_previous`` defaults
    to ``False``.  ``created_by_id`` is set by the service layer
    from the calling :class:`AdminUser`, never from the request
    body (so a forged request can't pin a policy on someone else).
    """

    model_config = _CLOSED_CONFIG

    name: str = Field(min_length=1, max_length=128)
    termination_severity: FlagSeverity = FlagSeverity.CRITICAL
    terminate_on_second_face: bool = True
    second_face_confirmation_frames: int = Field(default=3, ge=1, le=100)
    gaze_min_duration_ms: int = Field(default=800, ge=0, le=86_400_000)
    gaze_window_seconds: int = Field(default=300, ge=1, le=86_400)
    gaze_warning_limit: int = Field(default=3, ge=0, le=1_000)
    gaze_termination_limit: int = Field(default=8, ge=1, le=1_000)
    medium_score_termination_threshold: float = Field(default=10.0, ge=0.0)
    extra_rules: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    retire_previous: bool = False


class PolicyConfigResponse(BaseModel):
    """One row in the ``GET /admin/policy-config`` response."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: uuid.UUID
    name: str
    version: int
    is_active: bool
    retired_at: datetime | None
    termination_severity: FlagSeverity
    terminate_on_second_face: bool
    second_face_confirmation_frames: int
    gaze_min_duration_ms: int
    gaze_window_seconds: int
    gaze_warning_limit: int
    gaze_termination_limit: int
    medium_score_termination_threshold: float
    created_by_id: uuid.UUID | None
    created_at: datetime

    @classmethod
    def from_orm(cls, orm: Any) -> "PolicyConfigResponse":
        return cls(
            id=orm.id,
            name=orm.name,
            version=orm.version,
            is_active=orm.is_active,
            retired_at=orm.retired_at,
            termination_severity=orm.termination_severity,
            terminate_on_second_face=orm.terminate_on_second_face,
            second_face_confirmation_frames=(
                orm.second_face_confirmation_frames
            ),
            gaze_min_duration_ms=orm.gaze_min_duration_ms,
            gaze_window_seconds=orm.gaze_window_seconds,
            gaze_warning_limit=orm.gaze_warning_limit,
            gaze_termination_limit=orm.gaze_termination_limit,
            medium_score_termination_threshold=float(
                orm.medium_score_termination_threshold
            ),
            created_by_id=orm.created_by_id,
            created_at=orm.created_at,
        )


# ---------------------------------------------------------------------------
# /admin/accommodation-exemptions
# ---------------------------------------------------------------------------


class CreateExemptionRequest(BaseModel):
    """The body the admin sends to ``POST /admin/accommodation-exemptions``.

    ``approved_by_admin_id`` is set by the service layer from the
    calling :class:`AdminUser`.  The original ``approved_by`` string
    column is preserved in the v1 schema for backward compatibility;
    the service layer writes the same value to it (``claims.subject``
    from the session token) so legacy readers see the same string.
    """

    model_config = _CLOSED_CONFIG

    participant_id: uuid.UUID
    exam_reference: str = Field(min_length=1, max_length=512)
    object_class: str = Field(min_length=1, max_length=128)
    approval_reason: str = Field(min_length=1)
    effective_at: datetime
    expires_at: datetime | None = None


class AccommodationExemptionResponse(BaseModel):
    """One row in the ``GET /admin/accommodation-exemptions`` response."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: uuid.UUID
    participant_id: uuid.UUID
    exam_reference: str
    object_class: str
    approved_by: str
    approved_by_admin_id: uuid.UUID | None
    approval_reason: str
    effective_at: datetime
    expires_at: datetime | None
    created_at: datetime

    @classmethod
    def from_orm(cls, orm: Any) -> "AccommodationExemptionResponse":
        return cls(
            id=orm.id,
            participant_id=orm.participant_id,
            exam_reference=orm.exam_reference,
            object_class=orm.object_class,
            approved_by=orm.approved_by,
            approved_by_admin_id=orm.approved_by_admin_id,
            approval_reason=orm.approval_reason,
            effective_at=orm.effective_at,
            expires_at=orm.expires_at,
            created_at=orm.created_at,
        )


# ---------------------------------------------------------------------------
# /admin/flags/{session_id}  +  /admin/flags/{flag_id}/review
# ---------------------------------------------------------------------------


class EvidenceArtifactResponse(BaseModel):
    """The evidence-artifact shape included in
    :class:`FlagReviewResponse`."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: uuid.UUID
    kind: EvidenceKind
    storage_uri: str
    content_sha256: str
    media_type: str
    byte_size: int
    capture_started_at: datetime
    capture_ended_at: datetime | None
    retention_expires_at: datetime

    @classmethod
    def from_orm(cls, orm: Any) -> "EvidenceArtifactResponse":
        return cls(
            id=orm.id,
            kind=orm.kind,
            storage_uri=orm.storage_uri,
            content_sha256=orm.content_sha256,
            media_type=orm.media_type,
            byte_size=orm.byte_size,
            capture_started_at=orm.capture_started_at,
            capture_ended_at=orm.capture_ended_at,
            retention_expires_at=orm.retention_expires_at,
        )


class ProctorReviewResponse(BaseModel):
    """One existing review on a flag (zero or more per flag)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: uuid.UUID
    reviewer_reference: str
    reviewer_admin_id: uuid.UUID | None
    decision: ReviewDecision
    notes: str | None
    created_at: datetime

    @classmethod
    def from_orm(cls, orm: Any) -> "ProctorReviewResponse":
        return cls(
            id=orm.id,
            reviewer_reference=orm.reviewer_reference,
            reviewer_admin_id=orm.reviewer_admin_id,
            decision=orm.decision,
            notes=orm.notes,
            created_at=orm.created_at,
        )


class FlagReviewResponse(BaseModel):
    """One row in the ``GET /admin/flags/{session_id}`` response."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: uuid.UUID
    exam_session_id: uuid.UUID
    policy_config_id: uuid.UUID
    rule_code: str
    severity: FlagSeverity
    status: str
    confidence_score: float
    confidence_lower: float
    confidence_upper: float
    triggered_termination: bool
    suppressed_by_exemption_id: uuid.UUID | None
    detail: dict[str, Any]
    created_at: datetime
    resolved_at: datetime | None
    evidence_artifacts: list[EvidenceArtifactResponse]
    reviews: list[ProctorReviewResponse]

    @classmethod
    def from_orm(
        cls, orm: Any, *, include_suppressed: bool = True
    ) -> "FlagReviewResponse | None":
        suppressed = orm.suppressed_by_exemption_id is not None
        if suppressed and not include_suppressed:
            return None
        return cls(
            id=orm.id,
            exam_session_id=orm.exam_session_id,
            policy_config_id=orm.policy_config_id,
            rule_code=orm.rule_code,
            severity=orm.severity,
            status=orm.status.value,
            confidence_score=orm.confidence_score,
            confidence_lower=orm.confidence_lower,
            confidence_upper=orm.confidence_upper,
            triggered_termination=orm.triggered_termination,
            suppressed_by_exemption_id=orm.suppressed_by_exemption_id,
            detail=dict(orm.detail or {}),
            created_at=orm.created_at,
            resolved_at=orm.resolved_at,
            evidence_artifacts=[
                EvidenceArtifactResponse.from_orm(a)
                for a in orm.evidence_artifacts
            ],
            reviews=[
                ProctorReviewResponse.from_orm(r) for r in orm.reviews
            ],
        )


class FlagListResponse(BaseModel):
    """The full response from ``GET /admin/flags/{session_id}``."""

    model_config = _CLOSED_CONFIG

    session_id: uuid.UUID
    flags: list[FlagReviewResponse]
    omitted_suppressed_count: int = Field(ge=0)


class CreateProctorReviewRequest(BaseModel):
    """The body the admin sends to
    ``POST /admin/flags/{flag_id}/review``.

    ``decision`` controls whether the session transitions to
    ``UNDER_REVIEW``: only ``UPHELD`` drives the transition.
    ``notes`` is optional.  The session-token subject is recorded in
    :attr:`ProctorReview.reviewer_reference` for audit; the
    :class:`AdminUser` row id is recorded in
    :attr:`ProctorReview.reviewer_admin_id`.
    """

    model_config = _CLOSED_CONFIG

    decision: ReviewDecision
    notes: str | None = Field(default=None, max_length=10_000)


class ProctorReviewCreatedResponse(BaseModel):
    """The 201 response from the review endpoint."""

    model_config = _CLOSED_CONFIG

    id: uuid.UUID
    flag_id: uuid.UUID
    decision: ReviewDecision
    session_status: str


# ---------------------------------------------------------------------------
# /sessions/{id}/flags/{flag_id}/evidence  (the deferred §7 gap)
# ---------------------------------------------------------------------------


class SealEvidenceApiRequest(BaseModel):
    """The body the client sends to seal an evidence artifact.

    The blob is supplied as ``bytes`` via FastAPI's ``UploadFile``
    parsing — this Pydantic model carries the *metadata* the client
    sends alongside it.  The blob itself is read off the
    ``UploadFile`` by the route handler.
    """

    model_config = _CLOSED_CONFIG

    artifact_type: Literal["frame", "clip", "audio", "event_export"]
    media_type: str = Field(min_length=1, max_length=128)
    capture_started_at: datetime
    capture_ended_at: datetime | None
    retention_expires_at: datetime | None = None  # route stamps the default


class SealEvidenceApiResponse(BaseModel):
    """The 200/201 response from the evidence seal endpoint."""

    model_config = _CLOSED_CONFIG

    evidence_artifact_id: uuid.UUID
    flag_id: uuid.UUID
    storage_uri: str
    content_sha256: str
    byte_size: int
    media_type: str


# ---------------------------------------------------------------------------
# Constants — the closed set of error codes the orchestration layer
# surfaces.  Mirrored here (and in :mod:`proctoring_engine.orchestration._routes`)
# so the routes module can resolve a code → HTTP status mapping without
# each route handler knowing the full mapping.
# ---------------------------------------------------------------------------


# Reference-materials policy is exported here only so callers can
# round-trip a string in a request body without importing the ORM enum.
__all__ = [
    "AccommodationExemptionResponse",
    "CreateExemptionRequest",
    "CreatePolicyConfigRequest",
    "CreateProctorReviewRequest",
    "ErrorBody",
    "ErrorResponse",
    "EvidenceArtifactResponse",
    "FlagListResponse",
    "FlagReviewResponse",
    "PolicyConfigResponse",
    "ProctorReviewCreatedResponse",
    "ProctorReviewResponse",
    "SealEvidenceApiRequest",
    "SealEvidenceApiResponse",
    "SessionStatusResponse",
    "TerminateRequest",
    "TerminateResponse",
    "ReferenceMaterialPolicy",  # re-exported for callers
]