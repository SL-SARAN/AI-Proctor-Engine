"""PostgreSQL-oriented ORM schema for the locked v1 proctoring specification."""

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
    String,
    Text,
    Uuid,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates


class SessionStatus(str, enum.Enum):
    CREATED = "created"
    ACTIVE = "active"
    COMPLETED = "completed"
    TERMINATED = "terminated"
    CANCELLED = "cancelled"


class ReferenceMaterialPolicy(str, enum.Enum):
    CLOSED_BOOK = "closed_book"
    OPEN_BOOK = "open_book"
    SPECIFIC_LIST = "specific_list"


class TelemetryModality(str, enum.Enum):
    BROWSER = "browser"
    FACE = "face"
    GAZE = "gaze"
    IDENTITY = "identity"
    OBJECT = "object"
    AUDIO = "audio"
    SYSTEM = "system"


class FlagSeverity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FlagStatus(str, enum.Enum):
    RAISED = "raised"
    ACKNOWLEDGED = "acknowledged"
    DISMISSED = "dismissed"
    CONFIRMED = "confirmed"


class EvidenceKind(str, enum.Enum):
    FRAME = "frame"
    CLIP = "clip"
    AUDIO = "audio"
    EVENT_EXPORT = "event_export"


class DeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"


class ReviewDecision(str, enum.Enum):
    UPHELD = "upheld"
    OVERTURNED = "overturned"
    ANNOTATED = "annotated"


class Base(DeclarativeBase):
    """Declarative metadata shared by the complete v1 data model."""


def enum_type(enum_class: type[enum.Enum], name: str) -> SqlEnum:
    """Persist stable enum values rather than Python member names."""

    return SqlEnum(
        enum_class,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        create_constraint=True,
        validate_strings=True,
    )


JsonPayload = JSONB().with_variant(JSON(), "sqlite")


class Participant(Base):
    """A test-taker identified by a stable LMS subject within an issuer."""

    __tablename__ = "participants"
    __table_args__ = (
        Index("ix_participants_lms_identity", "lti_issuer", "lms_user_reference", unique=True),
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
    """Configurable policy thresholds; avoids hard-coded termination logic."""

    __tablename__ = "policy_configs"
    __table_args__ = (
        CheckConstraint("gaze_min_duration_ms >= 0", name="ck_policy_gaze_min_duration_nonnegative"),
        CheckConstraint("gaze_window_seconds > 0", name="ck_policy_gaze_window_positive"),
        CheckConstraint("gaze_warning_limit >= 0", name="ck_policy_gaze_warning_nonnegative"),
        CheckConstraint("gaze_termination_limit > 0", name="ck_policy_gaze_termination_positive"),
        CheckConstraint(
            "gaze_warning_limit <= gaze_termination_limit",
            name="ck_policy_gaze_warning_before_termination",
        ),
        CheckConstraint("second_face_confirmation_frames > 0", name="ck_policy_confirmation_frames_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    termination_severity: Mapped[FlagSeverity] = mapped_column(
        enum_type(FlagSeverity, "flag_severity"), nullable=False, default=FlagSeverity.CRITICAL
    )
    terminate_on_second_face: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    second_face_confirmation_frames: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    gaze_min_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=800)
    gaze_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    gaze_warning_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    gaze_termination_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    extra_rules: Mapped[dict[str, Any]] = mapped_column(JsonPayload, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    exam_sessions: Mapped[list["ExamSession"]] = relationship(back_populates="policy_config")


class ExamSession(Base):
    """One auditable proctored attempt in an LMS exam context."""

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
        Index("ix_exam_sessions_lms_context", "lti_issuer", "lti_context_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("participants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    policy_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policy_configs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    lti_issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    lti_context_id: Mapped[str] = mapped_column(String(512), nullable=False)
    exam_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    attempt_reference: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    status: Mapped[SessionStatus] = mapped_column(
        enum_type(SessionStatus, "session_status"), nullable=False, default=SessionStatus.CREATED
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
    termination_record: Mapped["TerminationRecord | None"] = relationship(back_populates="exam_session")


class AccommodationExemption(Base):
    """Administrator-approved object-class exception, recorded before an exam."""

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
    approval_reason: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    participant: Mapped[Participant] = relationship(back_populates="accommodation_exemptions")


class EnrollmentReference(Base):
    """Enrollment image metadata and its protected face-embedding vector."""

    __tablename__ = "enrollment_references"
    __table_args__ = (
        CheckConstraint("embedding_dimensions > 0", name="ck_enrollment_embedding_dimensions_positive"),
        Index("ix_enrollment_references_participant_active", "participant_id", "revoked_at"),
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
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    participant: Mapped[Participant] = relationship(back_populates="enrollment_references")

    @validates("embedding")
    def validate_embedding(self, _: str, value: list[float]) -> list[float]:
        if not value:
            raise ValueError("embedding must contain at least one value")
        if any(not isinstance(item, (int, float)) or not math.isfinite(float(item)) for item in value):
            raise ValueError("embedding must contain only finite numeric values")
        return [float(item) for item in value]

    @validates("embedding_dimensions")
    def validate_embedding_dimensions(self, _: str, value: int) -> int:
        if value <= 0:
            raise ValueError("embedding_dimensions must be positive")
        return value


class TelemetryEvent(Base):
    """Raw timestamped reading emitted by one client or server modality."""

    __tablename__ = "telemetry_events"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_telemetry_confidence_range"),
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
    raw_value: Mapped[dict[str, Any]] = mapped_column(JsonPayload, nullable=False, default=dict)
    bounding_boxes: Mapped[list[dict[str, Any]]] = mapped_column(JsonPayload, nullable=False, default=list)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)

    exam_session: Mapped[ExamSession] = relationship(back_populates="telemetry_events")
    flag_links: Mapped[list["FlagTelemetryEvent"]] = relationship(back_populates="telemetry_event")

    @validates("confidence")
    def validate_confidence(self, _: str, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("confidence must be between 0 and 1 inclusive")
        return float(value)


class Flag(Base):
    """Fused policy decision supported by one or more telemetry readings."""

    __tablename__ = "flags"
    __table_args__ = (
        CheckConstraint("confidence_lower >= 0", name="ck_flag_confidence_lower_min"),
        CheckConstraint("confidence_upper <= 1", name="ck_flag_confidence_upper_max"),
        CheckConstraint(
            "confidence_lower <= confidence_score AND confidence_score <= confidence_upper",
            name="ck_flag_confidence_interval_contains_score",
        ),
        Index("ix_flags_session_severity_created", "exam_session_id", "severity", "created_at"),
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
    detail: Mapped[dict[str, Any]] = mapped_column(JsonPayload, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    exam_session: Mapped[ExamSession] = relationship(back_populates="flags")
    telemetry_links: Mapped[list["FlagTelemetryEvent"]] = relationship(
        back_populates="flag", cascade="all, delete-orphan", order_by="FlagTelemetryEvent.position"
    )
    evidence_artifacts: Mapped[list["EvidenceArtifact"]] = relationship(
        back_populates="flag", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["ProctorReview"]] = relationship(
        back_populates="flag", cascade="all, delete-orphan"
    )
    termination_records: Mapped[list["TerminationRecord"]] = relationship(back_populates="triggering_flag")

    @validates("confidence_score", "confidence_lower", "confidence_upper")
    def validate_flag_confidence(self, _: str, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("flag confidence values must be between 0 and 1 inclusive")
        return float(value)


class FlagTelemetryEvent(Base):
    """Ordered evidence trail from a fused flag back to raw telemetry."""

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
    """Stored evidence linked to a flag, with retention and integrity metadata."""

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
    capture_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capture_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    encryption_key_reference: Mapped[str | None] = mapped_column(String(512))
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    flag: Mapped[Flag] = relationship(back_populates="evidence_artifacts")


class TerminationRecord(Base):
    """Append-only audit record of an automatic session termination."""

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
        enum_type(DeliveryStatus, "delivery_status"), nullable=False, default=DeliveryStatus.PENDING
    )
    client_command_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lms_delivery_status: Mapped[DeliveryStatus] = mapped_column(
        enum_type(DeliveryStatus, "delivery_status"), nullable=False, default=DeliveryStatus.PENDING
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
    """Human review or override tied to a single fused flag."""

    __tablename__ = "proctor_reviews"
    __table_args__ = (Index("ix_proctor_reviews_flag_created", "flag_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    flag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("flags.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    decision: Mapped[ReviewDecision] = mapped_column(
        enum_type(ReviewDecision, "review_decision"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    flag: Mapped[Flag] = relationship(back_populates="reviews")


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

