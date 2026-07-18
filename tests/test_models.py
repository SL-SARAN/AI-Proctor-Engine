"""Boundary and relational-integrity tests required by the v1 specification."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from proctoring_engine.models import (
    ExamSession,
    Flag,
    FlagSeverity,
    FlagTelemetryEvent,
    ImmutableRecordError,
    Participant,
    PolicyConfig,
    TelemetryEvent,
    TelemetryModality,
    TerminationRecord,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_session(db_session: Session) -> tuple[ExamSession, PolicyConfig]:
    participant = Participant(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=f"student-{uuid.uuid4()}",
        display_name="Example Student",
    )
    policy = PolicyConfig(name=f"default-{uuid.uuid4()}")
    db_session.add_all([participant, policy])
    db_session.flush()

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


def test_session_rejects_end_before_start(db_session: Session) -> None:
    participant = Participant(lti_issuer="https://lms.example.edu", lms_user_reference="student-1")
    policy = PolicyConfig(name="ordering-policy")
    db_session.add_all([participant, policy])
    db_session.flush()
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
    policy = PolicyConfig(name="foreign-key-policy")
    db_session.add(policy)
    db_session.flush()
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


def test_policy_requires_warning_limit_not_to_exceed_termination_limit(db_session: Session) -> None:
    invalid_policy = PolicyConfig(
        name="invalid-gaze-policy",
        gaze_warning_limit=9,
        gaze_termination_limit=8,
    )
    db_session.add(invalid_policy)

    with pytest.raises(IntegrityError, match="ck_policy_gaze_warning_before_termination"):
        db_session.commit()


def test_flag_cannot_link_same_telemetry_twice(db_session: Session) -> None:
    exam_session, policy = make_session(db_session)
    telemetry = TelemetryEvent(
        exam_session_id=exam_session.id,
        modality=TelemetryModality.OBJECT,
        event_type="object_detected",
        occurred_at=utc_now(),
        confidence=0.95,
    )
    flag = Flag(
        exam_session_id=exam_session.id,
        policy_config_id=policy.id,
        rule_code="OBJECT_CELL_PHONE",
        severity=FlagSeverity.CRITICAL,
        confidence_score=0.95,
        confidence_lower=0.90,
        confidence_upper=0.99,
    )
    db_session.add_all([telemetry, flag])
    db_session.flush()
    db_session.add_all(
        [
            FlagTelemetryEvent(flag_id=flag.id, telemetry_event_id=telemetry.id, position=0),
            FlagTelemetryEvent(flag_id=flag.id, telemetry_event_id=telemetry.id, position=1),
        ]
    )

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_termination_record_rejects_orm_mutation(db_session: Session) -> None:
    exam_session, policy = make_session(db_session)
    flag = Flag(
        exam_session_id=exam_session.id,
        policy_config_id=policy.id,
        rule_code="SECOND_FACE_CONFIRMED",
        severity=FlagSeverity.CRITICAL,
        confidence_score=0.99,
        confidence_lower=0.95,
        confidence_upper=1.0,
    )
    db_session.add(flag)
    db_session.flush()
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

