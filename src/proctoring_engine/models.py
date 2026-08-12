"""PostgreSQL-oriented ORM schema for the locked v1 proctoring specification.

This module is the single source of truth for the persisted data model. It
implements the entities defined in ``docs/01-data-models-design.md`` against
PostgreSQL 15+ and is used by Alembic migrations, the FastAPI service, and
the test suite.

Invariants enforced here (cross-referenced to the design docs):

* ``Participant`` identity is scoped by ``(lti_issuer, lms_user_reference)``
  per the LTI 1.3 launch contract (``docs/02-ingestion-layer-design.md`` §1).
* ``ExamSession.policy_config_id`` references a versioned snapshot of
  ``PolicyConfig`` so a mid-semester policy change does not silently mutate
  the rules under which an already-run session is interpreted
  (``docs/01`` ``PolicyConfig`` notes).
* ``Flag`` and ``TerminationRecord`` rows are append-only. ``TerminationRecord``
  is enforced both at the ORM level and (in the initial migration) at the
  PostgreSQL trigger level. ``Flag`` immutability was added in the audit
  reconciliation migration ``20260718_0002`` to match the spec's audit story
  for flags (corrections land in ``ProctorReview``, never in the flag itself).
* Confidence values are constrained to ``[0.0, 1.0]`` at the SQL level
  (``ck_telemetry_confidence_range``) and at the ORM level
  (``TelemetryEvent.validate_confidence``,
  ``Flag.validate_flag_confidence``). The two layers are belt-and-suspenders,
  not redundant: a bypass of the ORM still hits the SQL constraint.
* ``ExamSession.accumulated_medium_score`` carries the running weighted
  total for the accumulated-score termination path proposed in
  ``docs/05-fusion-flagging-engine-design.md`` Path 3.
* ``Flag.triggered_termination`` is the single source of truth for "this flag
  fired the kill-switch" — the orchestration layer reads it, not
  ``severity == CRITICAL``, because policy may set a non-CRITICAL severity
  as the termination trigger in the future.
* ``Flag.suppressed_by_exemption_id`` records the *exemption that acted* on
  the flag, never silently drops a detection — see
  ``docs/05`` §"Exemption suppression".
* ``EvidenceArtifact.flag_id`` is unique because the v1 spec is explicit
  that "one primary artifact per flag" is the model; adding more per flag
  is a schema change, not a row change.
* ``EnrollmentReference.embedding_model_version`` exists so a model upgrade
  can invalidate stale embeddings and force re-enrollment
  (``docs/01`` ``EnrollmentReference``).
* ``AdminUser`` is the structured identity model for administrators,
  proctors, and instructors. It replaces the free-form string references
  in ``AccommodationExemption.approved_by``, ``PolicyConfig.created_by``,
  and ``ProctorReview.reviewer_reference`` with FK-backed identities
  scoped by ``(lti_issuer, lms_user_reference)`` — the same natural key
  used for ``Participant``. The original string columns are preserved for
  backward compatibility; new writes populate the FK.
"""

from __future__ import annotations

import enum
import math
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    validates,
)


class SessionStatus(str, enum.Enum):
    """Lifecycle states for an exam session.

    The state machine is defined in ``docs/07-api-orchestration-design.md``
    §2. Only the fusion engine or an admin can move ``ACTIVE → TERMINATED``;
    every other transition is either student-driven (``COMPLETED``) or
    admin-driven (``UNDER_REVIEW``).  ``REINSTATED`` is the
    ``TERMINATED → REINSTATED`` fast-track-undo path for the
    accumulated-score termination (see ``05-fusion-flagging-engine-design.md``
    §Path 3 and ``01-data-models-design.md`` §"SessionStatus").
    """

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    TERMINATED = "terminated"
    UNDER_REVIEW = "under_review"
    REINSTATED = "reinstated"


class MediumScoreAction(str, enum.Enum):
    """What happens when the accumulated medium-score threshold is crossed.

    ``AUTO_TERMINATE`` is the legacy default — the kill-switch fires
    immediately through the existing path.  ``FLAG_FOR_REVIEW`` keeps
    the session live but raises a ``CRITICAL`` flag that must be
    reviewed by a live proctor before any further action.

    Per the design doc §Path 3, the action is admin-configurable via
    ``PolicyConfig.medium_score_action`` and is set ahead of the
    exam, not adjustable mid-session.
    """

    AUTO_TERMINATE = "auto_terminate"
    FLAG_FOR_REVIEW = "flag_for_review"


class LivenessAction(str, enum.Enum):
    """How the fusion engine treats a failed liveness check.

    ``CRITICAL_TERMINATE`` raises a ``CRITICAL`` flag with
    ``triggered_termination=True`` — the kill-switch fires immediately.
    ``MEDIUM_ACCUMULATE`` raises a ``MEDIUM`` flag that contributes to
    the accumulated-score path.

    Per the design doc §7, the action is admin-configurable via
    ``PolicyConfig.liveness_check_action`` and is required to be set
    explicitly when ``liveness_check_enabled=True``.
    """

    CRITICAL_TERMINATE = "critical_terminate"
    MEDIUM_ACCUMULATE = "medium_accumulate"


class ReferenceMaterialPolicy(str, enum.Enum):
    """How the fusion engine treats a detected ``book`` object."""

    CLOSED_BOOK = "closed_book"
    OPEN_BOOK = "open_book"
    SPECIFIC_LIST = "specific_list"


class TelemetryModality(str, enum.Enum):
    """The six modalities covered by v1 plus ``system`` for non-modal events."""

    BROWSER = "browser"
    FACE = "face"
    GAZE = "gaze"
    IDENTITY = "identity"
    OBJECT = "object"
    AUDIO = "audio"
    LIVENESS = "liveness"
    SYSTEM = "system"


class FlagSeverity(str, enum.Enum):
    """Severity tier for a fused ``Flag`` row.

    ``CRITICAL`` flags may trigger an auto-termination depending on
    ``PolicyConfig.termination_severity``; ``MEDIUM`` and below contribute
    to the accumulated-score path.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FlagStatus(str, enum.Enum):
    """Workflow status for a ``Flag`` row.

    Flags are append-only at the row level (no field is updated after
    creation) — but a *new* flag with the same rule code can be raised in a
    later turn. The status taxonomy here describes the *workflow* state,
    not a field that is mutated; transitions are represented by a new
    ``ProctorReview`` decision row, not by updating this column on the
    original flag.
    """

    RAISED = "raised"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    OVERTURNED = "overturned"


class EvidenceKind(str, enum.Enum):
    """Type of evidence stored alongside a flag."""

    FRAME = "frame"
    CLIP = "clip"
    AUDIO = "audio"
    EVENT_EXPORT = "event_export"


class DeliveryStatus(str, enum.Enum):
    """Lifecycle of a side-effect message (kill-switch, LMS callback)."""

    PENDING = "pending"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"


class ReviewDecision(str, enum.Enum):
    """The four terminal outcomes a human proctor can record."""

    UPHELD = "upheld"
    OVERTURNED = "overturned"
    ANNOTATED = "annotated"
    NEEDS_MORE_INFO = "needs_more_info"


class AdminRole(str, enum.Enum):
    """Role tier for an administrative user.

    Derived from the LTI ``roles`` claim: an instructor-role launch routes
    to policy management and exemption approval; an admin-role launch adds
    system-level configuration; a proctor-role launch routes to the review
    queue. The role stored here is the *highest* applicable tier at the
    time the admin was first seen.
    """

    INSTRUCTOR = "instructor"
    ADMIN = "admin"
    PROCTOR = "proctor"


class Base(DeclarativeBase):
    """Declarative metadata shared by the complete v1 data model."""


def enum_type(enum_class: type[enum.Enum], name: str) -> SqlEnum:
    """Persist stable enum values rather than Python member names.

    ``values_callable`` emits the ``.value`` of each member so that the
    stored representation is independent of the Python attribute name and
    survives renames.
    """

    return SqlEnum(
        enum_class,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        create_constraint=True,
        validate_strings=True,
    )


JsonPayload = JSONB().with_variant(JSON(), "sqlite")


class AdminUser(Base):
    """Structured identity for administrators, proctors, and instructors.

    Resolves the open decision documented in ``docs/01-data-models-design.md``
    §"Open decision: admin / reviewer identity". The natural key is
    ``(lti_issuer, lms_user_reference)`` — the same shape used for
    ``Participant``, scoped so the same admin across two LMS platforms
    remains distinct.

    ``retired_at`` is the soft-delete mechanism: a retired admin is
    preserved for audit (``ON DELETE RESTRICT`` on the FK columns that
    reference this table) but cannot be assigned to new reviews or
    exemptions at the service layer.
    """

    __tablename__ = "admin_users"
    __table_args__ = (
        UniqueConstraint(
            "lti_issuer",
            "lms_user_reference",
            name="uq_admin_users_lms_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    lti_issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    lms_user_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(512))
    role: Mapped[AdminRole] = mapped_column(
        enum_type(AdminRole, "admin_role"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Reverse relationships — populated by FK columns on the referencing tables.
    created_policies: Mapped[list["PolicyConfig"]] = relationship(
        back_populates="created_by_admin",
        foreign_keys="PolicyConfig.created_by_id",
    )
    approved_exemptions: Mapped[list["AccommodationExemption"]] = relationship(
        back_populates="approved_by_admin",
        foreign_keys="AccommodationExemption.approved_by_admin_id",
    )
    reviewed_flags: Mapped[list["ProctorReview"]] = relationship(
        back_populates="reviewer_admin",
        foreign_keys="ProctorReview.reviewer_admin_id",
    )


class Participant(Base):
    """A test-taker identified by a stable LMS subject within an issuer.

    The (lti_issuer, lms_user_reference) pair is the natural identifier
    that LTI 1.3 delivers on every launch
    (``docs/02-ingestion-layer-design.md`` §1). Uniqueness on that pair is
    enforced at the index level so the same student across two course
    contexts in the same LMS still resolves to one row per context, while
    two issuers' student "12345" stay distinct.
    """

    __tablename__ = "participants"
    __table_args__ = (
        Index(
            "ix_participants_lms_identity",
            "lti_issuer",
            "lms_user_reference",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    lti_issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    lms_user_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(512))
    consent_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_notice_version: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    exam_sessions: Mapped[list["ExamSession"]] = relationship(back_populates="participant")
    enrollment_references: Mapped[list["EnrollmentReference"]] = relationship(
        back_populates="participant", cascade="all, delete-orphan"
    )
    accommodation_exemptions: Mapped[list["AccommodationExemption"]] = relationship(
        back_populates="participant", cascade="all, delete-orphan"
    )


class PolicyConfig(Base):
    """Configurable policy thresholds; avoids hard-coded termination logic.

    Rows are append-only: a policy change creates a new version, never
    mutates the old one. Sessions reference a specific snapshot via
    ``ExamSession.policy_config_id`` so an institution tightening the gaze
    threshold mid-semester does not retroactively change the rules under
    which an already-completed session is interpreted.
    """

    __tablename__ = "policy_configs"
    __table_args__ = (
        CheckConstraint(
            "gaze_min_duration_ms >= 0",
            name="ck_policy_gaze_min_duration_nonnegative",
        ),
        CheckConstraint(
            "gaze_window_seconds > 0",
            name="ck_policy_gaze_window_positive",
        ),
        CheckConstraint(
            "gaze_warning_limit >= 0",
            name="ck_policy_gaze_warning_nonnegative",
        ),
        CheckConstraint(
            "gaze_termination_limit > 0",
            name="ck_policy_gaze_termination_positive",
        ),
        CheckConstraint(
            "gaze_warning_limit <= gaze_termination_limit",
            name="ck_policy_gaze_warning_before_termination",
        ),
        CheckConstraint(
            "gaze_min_duration_ms <= gaze_window_seconds * 1000",
            name="ck_policy_gaze_min_duration_within_window",
        ),
        CheckConstraint(
            "second_face_confirmation_frames > 0",
            name="ck_policy_confirmation_frames_positive",
        ),
        CheckConstraint(
            "liveness_confirmation_frames > 0",
            name="ck_policy_liveness_frames_positive",
        ),
        CheckConstraint(
            "identity_similarity_threshold >= 0 AND identity_similarity_threshold <= 1",
            name="ck_policy_identity_threshold_in_unit_interval",
        ),
        CheckConstraint(
            "identity_confirmation_frames > 0",
            name="ck_policy_identity_frames_positive",
        ),
        CheckConstraint(
            "audio_speech_ratio_threshold >= 0 AND audio_speech_ratio_threshold <= 1",
            name="ck_policy_audio_ratio_in_unit_interval",
        ),
        CheckConstraint(
            "medium_score_termination_threshold >= 0",
            name="ck_policy_medium_score_threshold_nonnegative",
        ),
        CheckConstraint(
            "liveness_score_threshold >= 0 AND liveness_score_threshold <= 1",
            name="ck_policy_liveness_threshold_in_unit_interval",
        ),
        # liveness_check_action must be set if liveness_check_enabled
        # is true.  The contrapositive (enabled=false → action=null)
        # is allowed; the design doc explicitly says the action is
        # unset when the feature is off, not "set to a default".
        CheckConstraint(
            "(liveness_check_enabled = false) OR (liveness_check_action IS NOT NULL)",
            name="ck_policy_liveness_action_when_enabled",
        ),
        # Partial unique index: a ``name`` is unique among the rows that
        # are currently active and not retired.  Retired policies
        # (``is_active = false`` or ``retired_at IS NOT NULL``) keep
        # their ``name`` slot for historical-reference / audit-trail
        # reasons — the same name can therefore appear once as an
        # active policy and again as a retired version of a prior
        # policy.  Replaces the unconditional ``unique=True`` on
        # ``name`` that was added at the v1 initial-schema commit and
        # turned out to prevent exactly the versioning workflow the
        # schema's ``is_active`` / ``retired_at`` columns were
        # designed to support.
        Index(
            "uq_policy_configs_active_name",
            "name",
            unique=True,
            postgresql_where=text("is_active = true AND retired_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    termination_severity: Mapped[FlagSeverity] = mapped_column(
        enum_type(FlagSeverity, "flag_severity"),
        nullable=False,
        default=FlagSeverity.CRITICAL,
    )
    terminate_on_second_face: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    second_face_confirmation_frames: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    gaze_min_duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=800
    )
    gaze_window_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300
    )
    gaze_warning_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    gaze_termination_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8
    )
    medium_score_termination_threshold: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, default=10.0
    )
    medium_score_action: Mapped[MediumScoreAction] = mapped_column(
        enum_type(MediumScoreAction, "medium_score_action"),
        nullable=False,
        default=MediumScoreAction.AUTO_TERMINATE,
    )
    liveness_check_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    liveness_check_action: Mapped[LivenessAction | None] = mapped_column(
        enum_type(LivenessAction, "liveness_action"),
        nullable=True,
        default=None,
    )
    liveness_score_threshold: Mapped[float] = mapped_column(
        Numeric(5, 4), nullable=False, default=0.5
    )
    liveness_confirmation_frames: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    identity_similarity_threshold: Mapped[float] = mapped_column(
        Numeric(5, 4), nullable=False, default=0.6
    )
    identity_confirmation_frames: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    audio_noise_floor_dbfs: Mapped[float] = mapped_column(
        Float, nullable=False, default=-30.0
    )
    audio_speech_ratio_threshold: Mapped[float] = mapped_column(
        Numeric(5, 4), nullable=False, default=0.3
    )
    extra_rules: Mapped[dict[str, Any]] = mapped_column(
        JsonPayload, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT")
    )

    exam_sessions: Mapped[list["ExamSession"]] = relationship(
        back_populates="policy_config"
    )
    created_by_admin: Mapped["AdminUser | None"] = relationship(
        back_populates="created_policies",
        foreign_keys=[created_by_id],
    )

    @validates("medium_score_termination_threshold")
    def validate_medium_score_threshold(self, _: str, value: float) -> float:
        if value < 0:
            raise ValueError("medium_score_termination_threshold must be >= 0")
        return float(value)


class ExamSession(Base):
    """One auditable proctored attempt in an LMS exam context.

    Holds the lifecycle status, the immutable policy snapshot reference,
    the consent timestamp (gating all persistence of telemetry and
    evidence), and the running accumulated-medium-score counter used by
    the fusion engine's Path 3.
    """

    __tablename__ = "exam_sessions"
    __table_args__ = (
        CheckConstraint(
            "ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at",
            name="ck_exam_session_timestamp_order",
        ),
        CheckConstraint(
            "retention_expires_at IS NULL OR started_at IS NULL OR retention_expires_at >= started_at",
            name="ck_exam_session_retention_after_start",
        ),
        CheckConstraint(
            "accumulated_medium_score >= 0",
            name="ck_exam_session_medium_score_nonnegative",
        ),
        Index(
            "ix_exam_sessions_lms_context",
            "lti_issuer",
            "lti_context_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("participants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    policy_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policy_configs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    lti_issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    lti_context_id: Mapped[str] = mapped_column(String(512), nullable=False)
    exam_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    attempt_reference: Mapped[str] = mapped_column(
        String(512), nullable=False, unique=True
    )
    status: Mapped[SessionStatus] = mapped_column(
        enum_type(SessionStatus, "session_status"),
        nullable=False,
        default=SessionStatus.PENDING,
    )
    allowed_reference_materials: Mapped[ReferenceMaterialPolicy] = mapped_column(
        enum_type(ReferenceMaterialPolicy, "reference_material_policy"),
        nullable=False,
        default=ReferenceMaterialPolicy.CLOSED_BOOK,
    )
    permitted_material_details: Mapped[dict[str, Any]] = mapped_column(
        JsonPayload, nullable=False, default=dict
    )
    consent_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accumulated_medium_score: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, default=0
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    participant: Mapped[Participant] = relationship(back_populates="exam_sessions")
    policy_config: Mapped[PolicyConfig] = relationship(back_populates="exam_sessions")
    telemetry_events: Mapped[list["TelemetryEvent"]] = relationship(
        back_populates="exam_session", cascade="all, delete-orphan"
    )
    flags: Mapped[list["Flag"]] = relationship(
        back_populates="exam_session", cascade="all, delete-orphan"
    )
    termination_record: Mapped["TerminationRecord | None"] = relationship(
        back_populates="exam_session"
    )

    @validates("accumulated_medium_score")
    def validate_accumulated_medium_score(self, _: str, value: float) -> float:
        if value < 0:
            raise ValueError("accumulated_medium_score must be >= 0")
        return float(value)


class AccommodationExemption(Base):
    """Administrator-approved object-class exception, recorded before an exam.

    In v1 the fusion engine's exemption suppression logic checks this table
    for every object-detection flag even though the v1 object-detection
    scope does not yet include earbuds or smartwatches. The logic is wired
    now so the moment those classes are added it is a config change, not
    a code change.
    """

    __tablename__ = "accommodation_exemptions"
    __table_args__ = (
        CheckConstraint(
            "expires_at IS NULL OR effective_at IS NULL OR expires_at > effective_at",
            name="ck_exemption_expiry_after_effective",
        ),
        Index(
            "ix_exemptions_participant_class_active",
            "participant_id",
            "object_class",
            "effective_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("participants.id", ondelete="RESTRICT"), nullable=False
    )
    exam_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    object_class: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(512), nullable=False)
    approved_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT")
    )
    approval_reason: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    participant: Mapped[Participant] = relationship(
        back_populates="accommodation_exemptions"
    )
    approved_by_admin: Mapped["AdminUser | None"] = relationship(
        back_populates="approved_exemptions",
        foreign_keys=[approved_by_admin_id],
    )


class EnrollmentReference(Base):
    """Enrollment image metadata and its protected face-embedding vector.

    ``embedding`` is stored as a JSONB float array in v1 — the design
    tradeoff is recorded in ``docs/01`` §"Open decision — embedding storage".
    The ``embedding_model_version`` column exists so a model upgrade can
    invalidate stale embeddings and force re-enrollment; the matched pair
    of (model_name, embedding_model_version) is what the identity-match
    module will use to decide whether an existing enrollment is usable.
    """

    __tablename__ = "enrollment_references"
    __table_args__ = (
        CheckConstraint(
            "embedding_dimensions > 0",
            name="ck_enrollment_embedding_dimensions_positive",
        ),
        Index(
            "ix_enrollment_references_participant_active",
            "participant_id",
            "revoked_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    storage_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    embedding: Mapped[list[float]] = mapped_column(JsonPayload, nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    embedding_model_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="unknown"
    )
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    participant: Mapped[Participant] = relationship(back_populates="enrollment_references")

    @validates("embedding")
    def validate_embedding(self, _: str, value: list[float]) -> list[float]:
        if not value:
            raise ValueError("embedding must contain at least one value")
        if any(
            not isinstance(item, (int, float)) or not math.isfinite(float(item))
            for item in value
        ):
            raise ValueError("embedding must contain only finite numeric values")
        return [float(item) for item in value]

    @validates("embedding_dimensions")
    def validate_embedding_dimensions(self, _: str, value: int) -> int:
        if value <= 0:
            raise ValueError("embedding_dimensions must be positive")
        return value

    @validates("embedding_model_version")
    def validate_embedding_model_version(self, _: str, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("embedding_model_version must be a non-empty string")
        return value.strip()


class TelemetryEvent(Base):
    """Raw timestamped reading emitted by one client or server modality.

    The ``confidence`` field is constrained to ``[0.0, 1.0]`` at both the
    SQL level (``ck_telemetry_confidence_range``) and the ORM level
    (``validate_confidence``). The two layers are belt-and-suspenders: a
    bypass of the ORM still hits the SQL constraint.
    """

    __tablename__ = "telemetry_events"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_telemetry_confidence_range",
        ),
        Index("ix_telemetry_session_occurred", "exam_session_id", "occurred_at"),
        Index("ix_telemetry_session_modality", "exam_session_id", "modality"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exam_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="CASCADE"), nullable=False
    )
    modality: Mapped[TelemetryModality] = mapped_column(
        enum_type(TelemetryModality, "telemetry_modality"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    raw_value: Mapped[dict[str, Any]] = mapped_column(
        JsonPayload, nullable=False, default=dict
    )
    bounding_boxes: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonPayload, nullable=False, default=list
    )
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)

    exam_session: Mapped[ExamSession] = relationship(back_populates="telemetry_events")
    flag_links: Mapped[list["FlagTelemetryEvent"]] = relationship(
        back_populates="telemetry_event"
    )

    @validates("confidence")
    def validate_confidence(self, _: str, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("confidence must be between 0 and 1 inclusive")
        return float(value)


class Flag(Base):
    """Fused policy decision supported by one or more telemetry readings.

    ``Flag`` rows are append-only at the row level. The
    ``confidence_interval`` triple — ``(confidence_lower, confidence_score,
    confidence_upper)`` — is the statistical interval, not a point estimate
    (see ``docs/04-inference-modules-design.md`` §2 for how identity-match
    produces it; the same shape applies to all modalities).
    """

    __tablename__ = "flags"
    __table_args__ = (
        CheckConstraint("confidence_lower >= 0", name="ck_flag_confidence_lower_min"),
        CheckConstraint("confidence_upper <= 1", name="ck_flag_confidence_upper_max"),
        CheckConstraint(
            "confidence_lower <= confidence_score AND confidence_score <= confidence_upper",
            name="ck_flag_confidence_interval_contains_score",
        ),
        Index(
            "ix_flags_session_severity_created",
            "exam_session_id",
            "severity",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exam_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="CASCADE"), nullable=False
    )
    policy_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policy_configs.id", ondelete="RESTRICT"), nullable=False
    )
    rule_code: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[FlagSeverity] = mapped_column(
        enum_type(FlagSeverity, "flag_severity"), nullable=False
    )
    status: Mapped[FlagStatus] = mapped_column(
        enum_type(FlagStatus, "flag_status"), nullable=False, default=FlagStatus.RAISED
    )
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_lower: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_upper: Mapped[float] = mapped_column(Float, nullable=False)
    triggered_termination: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    suppressed_by_exemption_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accommodation_exemptions.id", ondelete="RESTRICT")
    )
    detail: Mapped[dict[str, Any]] = mapped_column(
        JsonPayload, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    exam_session: Mapped[ExamSession] = relationship(back_populates="flags")
    telemetry_links: Mapped[list["FlagTelemetryEvent"]] = relationship(
        back_populates="flag",
        cascade="all, delete-orphan",
        order_by="FlagTelemetryEvent.position",
    )
    evidence_artifacts: Mapped[list["EvidenceArtifact"]] = relationship(
        back_populates="flag", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["ProctorReview"]] = relationship(
        back_populates="flag", cascade="all, delete-orphan"
    )
    termination_records: Mapped[list["TerminationRecord"]] = relationship(
        back_populates="triggering_flag"
    )
    suppressing_exemption: Mapped["AccommodationExemption | None"] = relationship(
        back_populates="suppressed_flags",
        foreign_keys=[suppressed_by_exemption_id],
    )

    @validates("confidence_score", "confidence_lower", "confidence_upper")
    def validate_flag_confidence(self, _: str, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("flag confidence values must be between 0 and 1 inclusive")
        return float(value)


class FlagTelemetryEvent(Base):
    """Ordered evidence trail from a fused flag back to raw telemetry.

    The composite primary key ``(flag_id, telemetry_event_id)`` enforces
    the "no duplicate link of the same telemetry to one flag" invariant
    at the schema level (``docs/08-test-strategy-design.md``).
    """

    __tablename__ = "flag_telemetry_events"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_flag_telemetry_position_nonnegative"),
    )

    flag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("flags.id", ondelete="CASCADE"), primary_key=True
    )
    telemetry_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("telemetry_events.id", ondelete="RESTRICT"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    flag: Mapped[Flag] = relationship(back_populates="telemetry_links")
    telemetry_event: Mapped[TelemetryEvent] = relationship(back_populates="flag_links")


class EvidenceArtifact(Base):
    """Stored evidence linked to a flag, with retention and integrity metadata.

    The v1 spec is explicit that "one primary artifact per flag" is the
    model. The unique constraint on ``flag_id`` enforces that; adding more
    per flag later is a schema change, not a row change.

    The ``content_sha256`` field is the integrity check; ``encryption_key_reference``
    is the KMS key identifier (or empty if the deployment does not use
    envelope encryption). ``retention_expires_at`` is what the deletion
    job acts on (``docs/06-evidence-audit-store-design.md`` §3).
    """

    __tablename__ = "evidence_artifacts"
    __table_args__ = (
        CheckConstraint("byte_size >= 0", name="ck_evidence_byte_size_nonnegative"),
        CheckConstraint(
            "capture_ended_at IS NULL OR capture_started_at IS NULL OR capture_ended_at >= capture_started_at",
            name="ck_evidence_capture_timestamp_order",
        ),
        CheckConstraint(
            "retention_expires_at >= capture_started_at",
            name="ck_evidence_retention_after_capture",
        ),
        UniqueConstraint("flag_id", name="uq_evidence_artifacts_one_per_flag"),
        Index("ix_evidence_retention_expiry", "retention_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    flag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("flags.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[EvidenceKind] = mapped_column(
        enum_type(EvidenceKind, "evidence_kind"), nullable=False
    )
    storage_uri: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    capture_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    capture_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    encryption_key_reference: Mapped[str | None] = mapped_column(String(512))
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    flag: Mapped[Flag] = relationship(back_populates="evidence_artifacts")


class TerminationRecord(Base):
    """Append-only audit record of an automatic session termination.

    The two delivery timestamps (``client_command_sent_at`` /
    ``client_acknowledged_at`` and ``lms_callback_sent_at`` /
    ``lms_callback_completed_at``) are the receipt-side proof of the
    kill-switch and the LMS callback having landed. The check constraints
    enforce that an acknowledgment cannot predate the send.
    """

    __tablename__ = "termination_records"
    __table_args__ = (
        CheckConstraint(
            "client_acknowledged_at IS NULL OR client_command_sent_at IS NULL OR client_acknowledged_at >= client_command_sent_at",
            name="ck_termination_client_ack_after_command",
        ),
        CheckConstraint(
            "lms_callback_completed_at IS NULL OR lms_callback_sent_at IS NULL OR lms_callback_completed_at >= lms_callback_sent_at",
            name="ck_termination_lms_callback_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exam_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    triggering_flag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("flags.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    client_delivery_status: Mapped[DeliveryStatus] = mapped_column(
        enum_type(DeliveryStatus, "delivery_status"),
        nullable=False,
        default=DeliveryStatus.PENDING,
    )
    client_command_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lms_delivery_status: Mapped[DeliveryStatus] = mapped_column(
        enum_type(DeliveryStatus, "delivery_status"),
        nullable=False,
        default=DeliveryStatus.PENDING,
    )
    lms_callback_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lms_callback_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    exam_session: Mapped[ExamSession] = relationship(back_populates="termination_record")
    triggering_flag: Mapped[Flag] = relationship(back_populates="termination_records")


class ProctorReview(Base):
    """Human review or override tied to a single fused flag.

    Sits alongside a ``Flag``; never edits it. The decision taxonomy
    (``UPHELD / OVERTURNED / ANNOTATED / NEEDS_MORE_INFO``) covers the
    full review surface: overturning, annotating without overturning, and
    flagging that the evidence is inconclusive are all first-class
    outcomes.
    """

    __tablename__ = "proctor_reviews"
    __table_args__ = (Index("ix_proctor_reviews_flag_created", "flag_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    flag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("flags.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    reviewer_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT")
    )
    decision: Mapped[ReviewDecision] = mapped_column(
        enum_type(ReviewDecision, "review_decision"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    flag: Mapped[Flag] = relationship(back_populates="reviews")
    reviewer_admin: Mapped["AdminUser | None"] = relationship(
        back_populates="reviewed_flags",
        foreign_keys=[reviewer_admin_id],
    )


# ----------------------------------------------------------------------
# Back-reference: AccommodationExemption.suppressed_flags
# ----------------------------------------------------------------------
# The forward reference ``Flag.suppressing_exemption`` is set above; the
# back-reference is attached here so the class is fully defined before the
# relationship is added. This is the standard SQLAlchemy pattern for
# two-way relationships on a class declared later in the file.
AccommodationExemption.suppressed_flags: Mapped[list["Flag"]] = relationship(
    "Flag",
    primaryjoin="AccommodationExemption.id == Flag.suppressed_by_exemption_id",
    back_populates="suppressing_exemption",
    viewonly=True,
)


class ImmutableRecordError(SQLAlchemyError):
    """Raised when application code attempts to change an append-only record."""


@event.listens_for(TerminationRecord, "before_update")
def reject_termination_record_update(*_: object) -> None:
    """Mirror the PostgreSQL trigger for ORM-mediated writes."""

    raise ImmutableRecordError("termination records are immutable")


@event.listens_for(TerminationRecord, "before_delete")
def reject_termination_record_delete(*_: object) -> None:
    """Mirror the PostgreSQL trigger for ORM-mediated deletes."""

    raise ImmutableRecordError("termination records are immutable")


@event.listens_for(Flag, "before_update")
def reject_flag_update(*_: object) -> None:
    """Mirror the PostgreSQL ``flag_immutable`` trigger for ORM-mediated writes.

    See ``migrations/versions/20260718_0002_audit_reconciliation.py`` for
    the database-level mirror. Corrections land in ``ProctorReview``,
    never in the flag itself.
    """

    raise ImmutableRecordError("flag records are immutable")


@event.listens_for(Flag, "before_delete")
def reject_flag_delete(*_: object) -> None:
    """Mirror the PostgreSQL ``flag_immutable`` trigger for ORM-mediated deletes."""

    raise ImmutableRecordError("flag records are immutable")
