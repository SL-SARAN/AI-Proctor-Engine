"""Boundary and relational-integrity tests for the v1 proctoring schema.

The test count below is the source of truth for ``docs/VERIFICATION_LOG.md``:

* Four ``@parametrize`` blocks contribute 16 cases (4 cases each).
* Nineteen individual tests.
* Total: 35 logical test cases.

These tests are SQLite-only and cover portable constraints
(confidence range, timestamp ordering, FK integrity, policy gaze ordering,
the one-artifact-per-flag unique constraint, ORM-level immutability on
both ``Flag`` and ``TerminationRecord``, and the ``AdminUser`` table
with its FK relationships to ``PolicyConfig``,
``AccommodationExemption``, and ``ProctorReview``). The PostgreSQL-only
constraints — enum types, the database-level immutability triggers,
JSONB indexes — are covered by the integration suite in
``tests/integration/``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from proctoring_engine.models import (
    AccommodationExemption,
    AdminRole,
    AdminUser,
    EnrollmentReference,
    ExamSession,
    Flag,
    FlagSeverity,
    FlagStatus,
    FlagTelemetryEvent,
    ImmutableRecordError,
    Participant,
    PolicyConfig,
    ProctorReview,
    ReferenceMaterialPolicy,
    ReviewDecision,
    SessionStatus,
    TelemetryEvent,
    TelemetryModality,
    TerminationRecord,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_participant_and_policy(
    db_session: Session,
    *,
    policy_name: str | None = None,
    policy_kwargs: dict[str, object] | None = None,
) -> tuple[Participant, PolicyConfig]:
    """Insert a fresh participant and a default policy, returning both."""

    participant = Participant(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=f"student-{uuid.uuid4()}",
        display_name="Example Student",
    )
    policy = PolicyConfig(
        name=policy_name or f"default-{uuid.uuid4()}",
        **(policy_kwargs or {}),
    )
    db_session.add_all([participant, policy])
    db_session.flush()
    return participant, policy


def make_session(
    db_session: Session,
    *,
    policy_kwargs: dict[str, object] | None = None,
) -> tuple[ExamSession, PolicyConfig]:
    """Insert a participant, policy, and a single pending exam session."""

    participant, policy = make_participant_and_policy(
        db_session, policy_kwargs=policy_kwargs
    )
    exam_session = ExamSession(
        participant_id=participant.id,
        policy_config_id=policy.id,
        lti_issuer=participant.lti_issuer,
        lti_context_id="course-101",
        exam_reference="exam-midterm",
        attempt_reference=f"attempt-{uuid.uuid4()}",
        started_at=utc_now(),
    )
    db_session.add(exam_session)
    db_session.flush()
    return exam_session, policy


def make_flag(
    db_session: Session,
    *,
    exam_session: ExamSession,
    policy: PolicyConfig,
    severity: FlagSeverity = FlagSeverity.CRITICAL,
    rule_code: str = "SECOND_FACE_CONFIRMED",
) -> Flag:
    """Insert a flag with a valid confidence interval."""

    flag = Flag(
        exam_session_id=exam_session.id,
        policy_config_id=policy.id,
        rule_code=rule_code,
        severity=severity,
        status=FlagStatus.RAISED,
        confidence_score=0.95,
        confidence_lower=0.90,
        confidence_upper=0.99,
    )
    db_session.add(flag)
    db_session.flush()
    return flag


# ----------------------------------------------------------------------
# Telemetry confidence boundaries (4 cases)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_telemetry_confidence_accepts_inclusive_boundaries(
    db_session: Session, confidence: float
) -> None:
    exam_session, _ = make_session(db_session)
    event = TelemetryEvent(
        exam_session_id=exam_session.id,
        modality=TelemetryModality.FACE,
        event_type="face_count",
        occurred_at=utc_now(),
        confidence=confidence,
    )
    db_session.add(event)
    db_session.commit()

    assert event.confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_telemetry_confidence_rejects_values_outside_range(confidence: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        TelemetryEvent(
            exam_session_id=uuid.uuid4(),
            modality=TelemetryModality.FACE,
            event_type="face_count",
            occurred_at=utc_now(),
            confidence=confidence,
        )


# ----------------------------------------------------------------------
# Flag confidence interval boundaries (4 cases)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lower", "score", "upper"),
    [
        (0.0, 0.0, 0.0),
        (1.0, 1.0, 1.0),
        (0.5, 0.5, 0.5),
        (0.1, 0.5, 0.9),
    ],
)
def test_flag_confidence_interval_accepts_valid_triples(
    db_session: Session, lower: float, score: float, upper: float
) -> None:
    exam_session, policy = make_session(db_session)
    flag = Flag(
        exam_session_id=exam_session.id,
        policy_config_id=policy.id,
        rule_code="INTERVAL_TEST",
        severity=FlagSeverity.LOW,
        confidence_score=score,
        confidence_lower=lower,
        confidence_upper=upper,
    )
    db_session.add(flag)
    db_session.commit()

    assert flag.confidence_lower == lower
    assert flag.confidence_score == score
    assert flag.confidence_upper == upper


@pytest.mark.parametrize(
    ("lower", "score", "upper"),
    [
        (-0.01, 0.5, 0.9),
        (0.1, -0.01, 0.9),
        (0.1, 0.5, 1.01),
        (0.9, 0.5, 0.1),  # interval does not contain the score
    ],
)
def test_flag_confidence_interval_rejects_invalid_triples(
    db_session: Session, lower: float, score: float, upper: float
) -> None:
    """A triple with any out-of-range value or a score outside the interval is rejected.

    Three of the four cases trip the ORM-level ``@validates`` decorator
    at construction time, raising ``ValueError``. The fourth case
    ``(0.9, 0.5, 0.1)`` — valid in range, but the score is not
    contained in the interval — passes the validator and is caught by
    the SQL check ``ck_flag_confidence_interval_contains_score`` at
    commit time, raising ``IntegrityError``. Both layers are intentional
    (belt-and-suspenders per the spec) and the test accepts either.
    """

    exam_session, policy = make_session(db_session)
    with pytest.raises((ValueError, IntegrityError)):
        flag = Flag(
            exam_session_id=exam_session.id,
            policy_config_id=policy.id,
            rule_code="INTERVAL_TEST_INVALID",
            severity=FlagSeverity.LOW,
            confidence_score=score,
            confidence_lower=lower,
            confidence_upper=upper,
        )
        db_session.add(flag)
        db_session.commit()


# ----------------------------------------------------------------------
# ExamSession timestamp ordering and FK integrity
# ----------------------------------------------------------------------


def test_session_rejects_end_before_start(db_session: Session) -> None:
    participant, policy = make_participant_and_policy(db_session)
    now = utc_now()
    invalid_session = ExamSession(
        participant_id=participant.id,
        policy_config_id=policy.id,
        lti_issuer=participant.lti_issuer,
        lti_context_id="course-101",
        exam_reference="exam-midterm",
        attempt_reference="attempt-ordering",
        started_at=now,
        ended_at=now - timedelta(seconds=1),
    )
    db_session.add(invalid_session)

    with pytest.raises(IntegrityError, match="ck_exam_session_timestamp_order"):
        db_session.commit()


def test_session_rejects_missing_participant_foreign_key(db_session: Session) -> None:
    _, policy = make_participant_and_policy(db_session)
    invalid_session = ExamSession(
        participant_id=uuid.uuid4(),
        policy_config_id=policy.id,
        lti_issuer="https://lms.example.edu",
        lti_context_id="course-101",
        exam_reference="exam-midterm",
        attempt_reference="attempt-missing-participant",
    )
    db_session.add(invalid_session)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_session_default_status_is_pending(db_session: Session) -> None:
    participant, policy = make_participant_and_policy(db_session)
    exam_session = ExamSession(
        participant_id=participant.id,
        policy_config_id=policy.id,
        lti_issuer=participant.lti_issuer,
        lti_context_id="course-101",
        exam_reference="exam-midterm",
        attempt_reference=f"attempt-{uuid.uuid4()}",
    )
    db_session.add(exam_session)
    db_session.commit()

    assert exam_session.status == SessionStatus.PENDING


# ----------------------------------------------------------------------
# PolicyConfig constraints
# ----------------------------------------------------------------------


def test_policy_requires_warning_limit_not_to_exceed_termination_limit(
    db_session: Session,
) -> None:
    invalid_policy = PolicyConfig(
        name="invalid-gaze-policy",
        gaze_warning_limit=9,
        gaze_termination_limit=8,
    )
    db_session.add(invalid_policy)

    with pytest.raises(
        IntegrityError, match="ck_policy_gaze_warning_before_termination"
    ):
        db_session.commit()


def test_policy_requires_gaze_min_duration_within_window(db_session: Session) -> None:
    """A 30s minimum against a 5s window is meaningless; reject it."""

    invalid_policy = PolicyConfig(
        name="invalid-min-duration-policy",
        gaze_min_duration_ms=30_000,
        gaze_window_seconds=5,
    )
    db_session.add(invalid_policy)

    with pytest.raises(
        IntegrityError, match="ck_policy_gaze_min_duration_within_window"
    ):
        db_session.commit()


def test_policy_medium_score_threshold_rejects_negative(db_session: Session) -> None:
    """A negative ``medium_score_termination_threshold`` is rejected.

    The ORM-level ``@validates`` decorator on
    ``PolicyConfig.medium_score_termination_threshold`` catches the
    negative value at construction time and raises ``ValueError``; the
    SQL check ``ck_policy_medium_score_threshold_nonnegative`` is the
    belt-and-suspenders layer. The test accepts either.
    """

    with pytest.raises((ValueError, IntegrityError)):
        invalid_policy = PolicyConfig(
            name=f"negative-threshold-policy-{uuid.uuid4()}",
            medium_score_termination_threshold=-1.0,
        )
        db_session.add(invalid_policy)
        db_session.commit()


# ----------------------------------------------------------------------
# PolicyConfig.name uniqueness (item 3)
# ----------------------------------------------------------------------


def test_policy_config_active_name_must_be_unique(db_session: Session) -> None:
    """Two active PolicyConfig rows with the same ``name`` are rejected.

    Replaces the original ``unique=True`` on ``name`` with a partial
    unique index over ``(name) WHERE is_active = true AND retired_at
    IS NULL``.  SQLite drops the WHERE clause (no partial-index
    support), so this test exercises the full unique-index fallback —
    which still correctly rejects the active-duplicate case that the
    partial index is designed to catch.
    """

    shared_name = f"shared-name-{uuid.uuid4()}"
    policy_a = PolicyConfig(name=shared_name)
    policy_b = PolicyConfig(name=shared_name)

    db_session.add(policy_a)
    db_session.flush()

    db_session.add(policy_b)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_policy_config_unique_constraint_name(db_session: Session) -> None:
    """The partial unique index is named ``uq_policy_configs_active_name``.

    Anchors the constraint name so future migrations don't accidentally
    drop or rename it.  The Index object is introspected from the
    table's ``__table_args__`` rather than via raw SQL because the
    underlying DBMS may store it as a UNIQUE INDEX (Postgres /
    SQLite) or as a UNIQUE constraint, depending on dialect.
    """

    from sqlalchemy import Index

    indexes = {
        i.name
        for ix in PolicyConfig.__table__.indexes
        for i in ([ix] if isinstance(ix, Index) else ix)  # noqa: PERF401
    }
    assert "uq_policy_configs_active_name" in indexes, (
        f"Expected unique index 'uq_policy_configs_active_name' on "
        f"policy_configs; got {sorted(indexes)!r}"
    )


# ----------------------------------------------------------------------
# ExamSession.accumulated_medium_score
# ----------------------------------------------------------------------


def test_exam_session_accumulated_medium_score_defaults_to_zero(
    db_session: Session,
) -> None:
    exam_session, _ = make_session(db_session)
    db_session.commit()

    assert exam_session.accumulated_medium_score == 0


def test_exam_session_rejects_negative_accumulated_medium_score(
    db_session: Session,
) -> None:
    """A negative ``accumulated_medium_score`` is rejected.

    Same belt-and-suspenders pattern as
    ``test_policy_medium_score_threshold_rejects_negative``: the
    ``@validates`` raises ``ValueError`` at construction, the SQL
    check ``ck_exam_session_medium_score_nonnegative`` raises
    ``IntegrityError`` at commit. The test accepts either.
    """

    participant, policy = make_participant_and_policy(db_session)
    with pytest.raises((ValueError, IntegrityError)):
        exam_session = ExamSession(
            participant_id=participant.id,
            policy_config_id=policy.id,
            lti_issuer=participant.lti_issuer,
            lti_context_id="course-101",
            exam_reference="exam-midterm",
            attempt_reference=f"attempt-{uuid.uuid4()}",
            accumulated_medium_score=-0.01,
        )
        db_session.add(exam_session)
        db_session.commit()


# ----------------------------------------------------------------------
# EnrollmentReference.embedding_model_version
# ----------------------------------------------------------------------


def test_enrollment_reference_embedding_model_version_is_required(
    db_session: Session,
) -> None:
    participant, _ = make_participant_and_policy(db_session)
    db_session.flush()

    # Empty string is rejected at the ORM level.
    with pytest.raises(ValueError, match="non-empty string"):
        enrollment = EnrollmentReference(
            participant_id=participant.id,
            storage_uri="s3://bucket/enrollment.jpg",
            content_sha256="a" * 64,
            embedding=[0.1, 0.2, 0.3],
            embedding_dimensions=3,
            model_name="facenet-test",
            embedding_model_version="",
        )
        db_session.add(enrollment)
        db_session.commit()


# ----------------------------------------------------------------------
# FlagTelemetryEvent dedupe
# ----------------------------------------------------------------------


def test_flag_cannot_link_same_telemetry_twice(db_session: Session) -> None:
    exam_session, policy = make_session(db_session)
    telemetry = TelemetryEvent(
        exam_session_id=exam_session.id,
        modality=TelemetryModality.OBJECT,
        event_type="object_detected",
        occurred_at=utc_now(),
        confidence=0.95,
    )
    flag = make_flag(
        db_session, exam_session=exam_session, policy=policy, rule_code="OBJECT_CELL_PHONE"
    )
    db_session.add(telemetry)
    db_session.flush()
    db_session.add_all(
        [
            FlagTelemetryEvent(flag_id=flag.id, telemetry_event_id=telemetry.id, position=0),
            FlagTelemetryEvent(flag_id=flag.id, telemetry_event_id=telemetry.id, position=1),
        ]
    )

    with pytest.raises(IntegrityError):
        db_session.commit()


# ----------------------------------------------------------------------
# EvidenceArtifact: one per flag (v1 invariant)
# ----------------------------------------------------------------------


def test_evidence_artifact_unique_per_flag(db_session: Session) -> None:
    """Two EvidenceArtifact rows on the same flag must be rejected.

    The v1 spec is explicit that "one primary artifact per flag" is the
    model; the unique constraint ``uq_evidence_artifacts_one_per_flag``
    enforces this. The integration test verifies the constraint name
    against the real engine; the unit test verifies the same invariant
    against SQLite, which reports the violation as
    ``UNIQUE constraint failed: evidence_artifacts.flag_id``.
    """

    exam_session, policy = make_session(db_session)
    flag = make_flag(db_session, exam_session=exam_session, policy=policy)
    db_session.flush()

    first = _make_evidence(flag.id)
    db_session.add(first)
    db_session.flush()

    second = _make_evidence(flag.id, suffix="b")
    db_session.add(second)

    with pytest.raises(IntegrityError, match="evidence_artifacts.flag_id"):
        db_session.commit()


def _make_evidence(flag_id: uuid.UUID, *, suffix: str = "a") -> "EvidenceArtifact":
    """Construct a minimal EvidenceArtifact for the unique-constraint test."""

    from proctoring_engine.models import EvidenceArtifact, EvidenceKind

    capture_started = utc_now()
    return EvidenceArtifact(
        flag_id=flag_id,
        kind=EvidenceKind.CLIP,
        storage_uri=f"s3://bucket/evidence/{flag_id}/{suffix}.webm",
        content_sha256=("a" * 63) + suffix,
        media_type="video/webm",
        byte_size=1024,
        capture_started_at=capture_started,
        retention_expires_at=capture_started + timedelta(days=30),
    )


# ----------------------------------------------------------------------
# Immutability
# ----------------------------------------------------------------------


def test_termination_record_rejects_orm_mutation(db_session: Session) -> None:
    exam_session, policy = make_session(db_session)
    flag = make_flag(db_session, exam_session=exam_session, policy=policy)
    termination = TerminationRecord(
        exam_session_id=exam_session.id,
        triggering_flag_id=flag.id,
        reason="confirmed second person",
    )
    db_session.add(termination)
    db_session.commit()

    termination.reason = "altered after the fact"
    with pytest.raises(ImmutableRecordError, match="immutable"):
        db_session.commit()
    db_session.rollback()


def test_flag_rejects_orm_mutation(db_session: Session) -> None:
    """Flag rows are append-only; corrections land in ProctorReview, not Flag."""

    exam_session, policy = make_session(db_session)
    flag = make_flag(db_session, exam_session=exam_session, policy=policy)
    db_session.commit()

    flag.severity = FlagSeverity.LOW
    with pytest.raises(ImmutableRecordError, match="immutable"):
        db_session.commit()
    db_session.rollback()


def test_flag_rejects_orm_delete(db_session: Session) -> None:
    exam_session, policy = make_session(db_session)
    flag = make_flag(db_session, exam_session=exam_session, policy=policy)
    db_session.commit()

    db_session.delete(flag)
    with pytest.raises(ImmutableRecordError, match="immutable"):
        db_session.commit()
    db_session.rollback()


# ----------------------------------------------------------------------
# Flag.triggered_termination default
# ----------------------------------------------------------------------


def test_flag_triggered_termination_defaults_to_false(db_session: Session) -> None:
    exam_session, policy = make_session(db_session)
    flag = make_flag(
        db_session,
        exam_session=exam_session,
        policy=policy,
        severity=FlagSeverity.MEDIUM,
        rule_code="GAZE_WARNING",
    )
    db_session.commit()

    assert flag.triggered_termination is False


# ----------------------------------------------------------------------
# Flag.suppressed_by_exemption_id round-trip
# ----------------------------------------------------------------------


def test_flag_suppressed_by_exemption_round_trips(db_session: Session) -> None:
    """A flag carries its ``suppressed_by_exemption_id`` at insertion time.

    The fusion engine decides whether an exemption suppresses a flag in
    the same atomic step that creates the flag — there is no later
    "go back and set the suppression" path because ``Flag`` rows are
    append-only (the immutability rule is enforced both at the ORM
    listener and at the PostgreSQL trigger). The test therefore
    constructs the flag with ``suppressed_by_exemption_id`` set, not
    via an update.
    """

    participant, policy = make_participant_and_policy(db_session)
    exam_session = ExamSession(
        participant_id=participant.id,
        policy_config_id=policy.id,
        lti_issuer=participant.lti_issuer,
        lti_context_id="course-101",
        exam_reference="exam-midterm",
        attempt_reference=f"attempt-{uuid.uuid4()}",
    )
    db_session.add(exam_session)
    db_session.flush()

    exemption = AccommodationExemption(
        participant_id=participant.id,
        exam_reference=exam_session.exam_reference,
        object_class="hearing_aid",
        approved_by="admin@example.edu",
        approval_reason="documented accommodation",
        effective_at=utc_now(),
    )
    db_session.add(exemption)
    db_session.flush()

    # Construct the flag with the suppression reference set. This is the
    # only legal way to populate ``suppressed_by_exemption_id``: any
    # later mutation of a Flag row is rejected by the immutability
    # listener / trigger.
    flag = Flag(
        exam_session_id=exam_session.id,
        policy_config_id=policy.id,
        rule_code="OBJECT_HEARING_AID",
        severity=FlagSeverity.MEDIUM,
        status=FlagStatus.RAISED,
        confidence_score=0.9,
        confidence_lower=0.8,
        confidence_upper=0.95,
        suppressed_by_exemption_id=exemption.id,
    )
    db_session.add(flag)
    db_session.commit()

    db_session.refresh(flag)
    assert flag.suppressed_by_exemption_id == exemption.id
    assert flag.suppressing_exemption is not None
    assert flag.suppressing_exemption.id == exemption.id
    assert flag.suppressing_exemption.object_class == "hearing_aid"


# ----------------------------------------------------------------------
# AdminUser: natural key uniqueness
# ----------------------------------------------------------------------


def test_admin_user_natural_key_uniqueness(db_session: Session) -> None:
    """Two AdminUser rows with the same (lti_issuer, lms_user_reference)
    must be rejected by the unique constraint."""

    admin_1 = AdminUser(
        lti_issuer="https://lms.example.edu",
        lms_user_reference="admin-001",
        display_name="Admin One",
        role=AdminRole.ADMIN,
    )
    db_session.add(admin_1)
    db_session.flush()

    admin_2 = AdminUser(
        lti_issuer="https://lms.example.edu",
        lms_user_reference="admin-001",
        display_name="Admin Duplicate",
        role=AdminRole.PROCTOR,
    )
    db_session.add(admin_2)

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ----------------------------------------------------------------------
# AdminUser: role round-trip
# ----------------------------------------------------------------------


def test_admin_user_role_round_trips(db_session: Session) -> None:
    """Each AdminRole value can be persisted and read back."""

    for role in AdminRole:
        admin = AdminUser(
            lti_issuer="https://lms.example.edu",
            lms_user_reference=f"admin-{role.value}-{uuid.uuid4()}",
            role=role,
        )
        db_session.add(admin)

    db_session.commit()

    admins = db_session.query(AdminUser).all()
    persisted_roles = {a.role for a in admins}
    assert persisted_roles == set(AdminRole)


# ----------------------------------------------------------------------
# AdminUser FK round-trips: PolicyConfig, AccommodationExemption,
# ProctorReview
# ----------------------------------------------------------------------


def test_policy_config_created_by_admin_round_trips(db_session: Session) -> None:
    """A PolicyConfig with created_by_id set to an AdminUser round-trips."""

    admin = AdminUser(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=f"admin-{uuid.uuid4()}",
        role=AdminRole.ADMIN,
    )
    db_session.add(admin)
    db_session.flush()

    policy = PolicyConfig(
        name=f"admin-policy-{uuid.uuid4()}",
        created_by_id=admin.id,
    )
    db_session.add(policy)
    db_session.commit()

    db_session.refresh(policy)
    assert policy.created_by_id == admin.id
    assert policy.created_by_admin is not None
    assert policy.created_by_admin.lms_user_reference == admin.lms_user_reference


def test_accommodation_exemption_approved_by_admin_round_trips(
    db_session: Session,
) -> None:
    """An AccommodationExemption with approved_by_admin_id round-trips."""

    admin = AdminUser(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=f"admin-{uuid.uuid4()}",
        role=AdminRole.INSTRUCTOR,
    )
    participant = Participant(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=f"student-{uuid.uuid4()}",
    )
    db_session.add_all([admin, participant])
    db_session.flush()

    exemption = AccommodationExemption(
        participant_id=participant.id,
        exam_reference="exam-midterm",
        object_class="hearing_aid",
        approved_by="admin@example.edu",
        approved_by_admin_id=admin.id,
        approval_reason="documented accommodation",
        effective_at=utc_now(),
    )
    db_session.add(exemption)
    db_session.commit()

    db_session.refresh(exemption)
    assert exemption.approved_by_admin_id == admin.id
    assert exemption.approved_by_admin is not None
    assert exemption.approved_by_admin.role == AdminRole.INSTRUCTOR


def test_proctor_review_reviewer_admin_round_trips(db_session: Session) -> None:
    """A ProctorReview with reviewer_admin_id round-trips."""

    admin = AdminUser(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=f"proctor-{uuid.uuid4()}",
        role=AdminRole.PROCTOR,
    )
    db_session.add(admin)
    db_session.flush()

    exam_session, policy = make_session(db_session)
    flag = make_flag(db_session, exam_session=exam_session, policy=policy)
    db_session.commit()

    review = ProctorReview(
        flag_id=flag.id,
        reviewer_reference="proctor@example.edu",
        reviewer_admin_id=admin.id,
        decision=ReviewDecision.UPHELD,
        notes="Confirmed cheating behavior.",
    )
    db_session.add(review)
    db_session.commit()

    db_session.refresh(review)
    assert review.reviewer_admin_id == admin.id
    assert review.reviewer_admin is not None
    assert review.reviewer_admin.role == AdminRole.PROCTOR


# ----------------------------------------------------------------------
# AdminUser: retired_at round-trip
# ----------------------------------------------------------------------


def test_admin_user_retired_at_round_trips(db_session: Session) -> None:
    """Setting retired_at persists correctly."""

    admin = AdminUser(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=f"admin-{uuid.uuid4()}",
        role=AdminRole.ADMIN,
        retired_at=utc_now(),
    )
    db_session.add(admin)
    db_session.commit()

    db_session.refresh(admin)
    assert admin.retired_at is not None


# ----------------------------------------------------------------------
# AdminUser: FK rejects nonexistent admin
# ----------------------------------------------------------------------


def test_admin_user_fk_rejects_nonexistent(db_session: Session) -> None:
    """A ProctorReview with a nonexistent reviewer_admin_id is rejected."""

    exam_session, policy = make_session(db_session)
    flag = make_flag(db_session, exam_session=exam_session, policy=policy)
    db_session.commit()

    review = ProctorReview(
        flag_id=flag.id,
        reviewer_reference="ghost@example.edu",
        reviewer_admin_id=uuid.uuid4(),  # Does not exist.
        decision=ReviewDecision.OVERTURNED,
    )
    db_session.add(review)

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
