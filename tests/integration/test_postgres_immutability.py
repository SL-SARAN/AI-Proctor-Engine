"""PostgreSQL integration tests for the v1 proctoring schema.

These tests verify the database-level invariants that SQLite cannot model:
the enum types created by SQLAlchemy, the ``flag_immutable`` and
``termination_record_immutable`` triggers, the JSONB columns, and the
foreign-key cascade behaviors.

The test module is skipped when ``INTEGRATION_DATABASE_URL`` is not set,
so the standard ``pytest`` invocation in development still runs only the
SQLite unit suite. In CI, the integration job sets
``INTEGRATION_DATABASE_URL`` to the GitHub Actions ``services.postgres``
host and runs this module explicitly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from proctoring_engine.models import (
    ExamSession,
    Flag,
    FlagSeverity,
    FlagStatus,
    Participant,
    PolicyConfig,
    TelemetryEvent,
    TelemetryModality,
    TerminationRecord,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_minimum(db_session: Session) -> tuple[ExamSession, PolicyConfig]:
    """Insert the minimum rows required to attach a flag to a session."""

    participant = Participant(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=f"student-{uuid.uuid4()}",
    )
    policy = PolicyConfig(name=f"policy-{uuid.uuid4()}")
    db_session.add_all([participant, policy])
    db_session.flush()

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
    return exam_session, policy


def _seed_flag(db_session: Session) -> Flag:
    """Insert a flag attached to a fresh session, ready for SQL mutations."""

    exam_session, policy = _seed_minimum(db_session)
    flag = Flag(
        exam_session_id=exam_session.id,
        policy_config_id=policy.id,
        rule_code="SECOND_FACE_CONFIRMED",
        severity=FlagSeverity.CRITICAL,
        status=FlagStatus.RAISED,
        confidence_score=0.95,
        confidence_lower=0.90,
        confidence_upper=0.99,
    )
    db_session.add(flag)
    db_session.commit()
    return flag


# ----------------------------------------------------------------------
# Schema introspection
# ----------------------------------------------------------------------


def test_session_status_enum_contains_under_review(
    integration_engine_session: Session,
) -> None:
    """The audit-reconciliation migration added ``under_review`` to the enum."""

    result = integration_engine_session.execute(
        text(
            "SELECT unnest(enum_range(NULL::session_status))::text AS value "
            "ORDER BY value"
        )
    )
    values = [row[0] for row in result]
    assert "under_review" in values
    assert "pending" in values
    assert "active" in values
    assert "completed" in values
    assert "terminated" in values
    # The legacy 'created' and 'cancelled' values must not have leaked through.
    assert "created" not in values
    assert "cancelled" not in values


def test_flag_status_enum_contains_overturned(
    integration_engine_session: Session,
) -> None:
    result = integration_engine_session.execute(
        text("SELECT unnest(enum_range(NULL::flag_status))::text AS value ORDER BY value")
    )
    values = [row[0] for row in result]
    assert "overturned" in values


def test_review_decision_enum_contains_needs_more_info(
    integration_engine_session: Session,
) -> None:
    result = integration_engine_session.execute(
        text(
            "SELECT unnest(enum_range(NULL::review_decision))::text AS value "
            "ORDER BY value"
        )
    )
    values = [row[0] for row in result]
    assert "needs_more_info" in values


# ----------------------------------------------------------------------
# flag_immutable trigger (database-level mirror of the ORM listener)
# ----------------------------------------------------------------------


def test_flag_immutable_trigger_rejects_update(
    integration_engine_session: Session,
) -> None:
    flag = _seed_flag(integration_engine_session)

    with pytest.raises(IntegrityError, match="flag records are immutable"):
        integration_engine_session.execute(
            text("UPDATE flags SET severity = 'low' WHERE id = :id"),
            {"id": flag.id},
        )
    integration_engine_session.rollback()


def test_flag_immutable_trigger_rejects_delete(
    integration_engine_session: Session,
) -> None:
    flag = _seed_flag(integration_engine_session)

    with pytest.raises(IntegrityError, match="flag records are immutable"):
        integration_engine_session.execute(
            text("DELETE FROM flags WHERE id = :id"),
            {"id": flag.id},
        )
    integration_engine_session.rollback()


# ----------------------------------------------------------------------
# termination_record_immutable trigger (existing, regression-tested)
# ----------------------------------------------------------------------


def test_termination_record_immutable_trigger_rejects_update(
    integration_engine_session: Session,
) -> None:
    flag = _seed_flag(integration_engine_session)
    termination = TerminationRecord(
        exam_session_id=flag.exam_session_id,
        triggering_flag_id=flag.id,
        reason="confirmed second person",
    )
    integration_engine_session.add(termination)
    integration_engine_session.commit()

    with pytest.raises(IntegrityError, match="termination_records are immutable"):
        integration_engine_session.execute(
            text("UPDATE termination_records SET reason = 'altered' WHERE id = :id"),
            {"id": termination.id},
        )
    integration_engine_session.rollback()


def test_termination_record_immutable_trigger_rejects_delete(
    integration_engine_session: Session,
) -> None:
    flag = _seed_flag(integration_engine_session)
    termination = TerminationRecord(
        exam_session_id=flag.exam_session_id,
        triggering_flag_id=flag.id,
        reason="confirmed second person",
    )
    integration_engine_session.add(termination)
    integration_engine_session.commit()

    with pytest.raises(IntegrityError, match="termination_records are immutable"):
        integration_engine_session.execute(
            text("DELETE FROM termination_records WHERE id = :id"),
            {"id": termination.id},
        )
    integration_engine_session.rollback()


# ----------------------------------------------------------------------
# Check constraints that SQLite cannot model
# ----------------------------------------------------------------------


def test_gaze_min_duration_within_window_is_enforced_by_postgres(
    integration_engine_session: Session,
) -> None:
    with pytest.raises(IntegrityError):
        invalid = PolicyConfig(
            name=f"invalid-window-{uuid.uuid4()}",
            gaze_min_duration_ms=30_000,
            gaze_window_seconds=5,
        )
        integration_engine_session.add(invalid)
        integration_engine_session.commit()
    integration_engine_session.rollback()


def test_one_evidence_artifact_per_flag_is_enforced_by_postgres(
    integration_engine_session: Session,
) -> None:
    """The unique constraint added in 20260718_0002 is honored by Postgres."""

    from proctoring_engine.models import EvidenceArtifact, EvidenceKind

    flag = _seed_flag(integration_engine_session)
    capture_started = utc_now()
    first = EvidenceArtifact(
        flag_id=flag.id,
        kind=EvidenceKind.CLIP,
        storage_uri=f"s3://bucket/evidence/{flag.id}/a.webm",
        content_sha256="a" * 64,
        media_type="video/webm",
        byte_size=1024,
        capture_started_at=capture_started,
        retention_expires_at=capture_started.replace(day=1),
    )
    integration_engine_session.add(first)
    integration_engine_session.commit()

    second = EvidenceArtifact(
        flag_id=flag.id,
        kind=EvidenceKind.CLIP,
        storage_uri=f"s3://bucket/evidence/{flag.id}/b.webm",
        content_sha256="b" * 64,
        media_type="video/webm",
        byte_size=2048,
        capture_started_at=capture_started,
        retention_expires_at=capture_started.replace(day=1),
    )
    integration_engine_session.add(second)
    with pytest.raises(IntegrityError, match="uq_evidence_artifacts_one_per_flag"):
        integration_engine_session.commit()
    integration_engine_session.rollback()


# ----------------------------------------------------------------------
# New column round-trips against the real engine
# ----------------------------------------------------------------------


def test_accumulated_medium_score_round_trips(integration_engine_session: Session) -> None:
    exam_session, _ = _seed_minimum(integration_engine_session)
    exam_session.accumulated_medium_score = 7.5
    integration_engine_session.commit()

    integration_engine_session.refresh(exam_session)
    assert exam_session.accumulated_medium_score == 7.5


def test_flag_triggered_termination_round_trips(
    integration_engine_session: Session,
) -> None:
    flag = _seed_flag(integration_engine_session)
    assert flag.triggered_termination is False

    # A direct UPDATE is rejected by the trigger; the only way to set the
    # value to true is by inserting a new flag with the right value. This
    # is the audit trail's guarantee.
    second = Flag(
        exam_session_id=flag.exam_session_id,
        policy_config_id=flag.policy_config_id,
        rule_code="GAZE_TERMINATION",
        severity=FlagSeverity.CRITICAL,
        status=FlagStatus.RAISED,
        confidence_score=0.95,
        confidence_lower=0.90,
        confidence_upper=0.99,
        triggered_termination=True,
    )
    integration_engine_session.add(second)
    integration_engine_session.commit()
    assert second.triggered_termination is True


# ----------------------------------------------------------------------
# Foreign-key behavior at the SQL level
# ----------------------------------------------------------------------


def test_flag_cannot_reference_nonexistent_policy(
    integration_engine_session: Session,
) -> None:
    """FK constraint on flags.policy_config_id is enforced at the SQL level."""

    with pytest.raises(IntegrityError):
        integration_engine_session.execute(
            text(
                "INSERT INTO flags ("
                "  id, exam_session_id, policy_config_id, rule_code, severity, status,"
                "  confidence_score, confidence_lower, confidence_upper, detail"
                ") VALUES ("
                "  :id, :session_id, :policy_id, 'TEST', 'low', 'raised',"
                "  0.5, 0.4, 0.6, '{}'::jsonb)"
            ),
            {
                "id": str(uuid.uuid4()),
                "session_id": str(uuid.uuid4()),
                "policy_id": str(uuid.uuid4()),
            },
        )
    integration_engine_session.rollback()


def test_referential_action_protects_termination_record(
    integration_engine_session: Session,
) -> None:
    """Deleting a flag with a termination record on it is blocked."""

    flag = _seed_flag(integration_engine_session)
    termination = TerminationRecord(
        exam_session_id=flag.exam_session_id,
        triggering_flag_id=flag.id,
        reason="confirmed second person",
    )
    integration_engine_session.add(termination)
    integration_engine_session.commit()

    # Postgres rejects the cascade delete on flags because the
    # termination_record FK uses ON DELETE RESTRICT.
    with pytest.raises((IntegrityError, ProgrammingError)):
        integration_engine_session.execute(
            text("DELETE FROM flags WHERE id = :id"),
            {"id": flag.id},
        )
    integration_engine_session.rollback()
