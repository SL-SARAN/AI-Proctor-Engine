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
from datetime import datetime, timedelta, timezone

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
    ProctorReview,
    ReviewDecision,
    TelemetryEvent,
    TelemetryModality,
    TerminationRecord,
)
from proctoring_engine.models import AdminRole, AdminUser


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
    """The ``session_status`` enum contains ``under_review`` from the Python
    ``SessionStatus`` enum (``Base.metadata.create_all`` in the initial
    migration). The audit-reconciliation migration does not add it."""

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

    # PostgreSQL's ``RAISE EXCEPTION`` from a trigger returns
    # SQLSTATE ``P0001`` (the generic ``raise_exception`` class), which
    # SQLAlchemy maps to ``ProgrammingError`` — not ``IntegrityError``,
    # which is reserved for SQLSTATE class 23 (constraint violations).
    # The ``RAISE`` message is preserved on the exception, so we
    # ``match=`` against it to assert the right trigger fired.
    with pytest.raises(ProgrammingError, match="flag records are immutable"):
        integration_engine_session.execute(
            text("UPDATE flags SET severity = 'low' WHERE id = :id"),
            {"id": flag.id},
        )
    integration_engine_session.rollback()


def test_flag_immutable_trigger_rejects_delete(
    integration_engine_session: Session,
) -> None:
    flag = _seed_flag(integration_engine_session)

    with pytest.raises(ProgrammingError, match="flag records are immutable"):
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

    with pytest.raises(ProgrammingError, match="termination_records are immutable"):
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

    with pytest.raises(ProgrammingError, match="termination_records are immutable"):
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
    """The ``uq_evidence_artifacts_one_per_flag`` unique constraint
    declared on the ``EvidenceArtifact`` model is honored by Postgres
    (emitted by the initial migration's ``Base.metadata.create_all``).
    """

    from proctoring_engine.models import EvidenceArtifact, EvidenceKind

    flag = _seed_flag(integration_engine_session)
    capture_started = utc_now()
    # Retention must be at or after capture_started (the
    # ``ck_evidence_retention_after_capture`` check on the
    # ``evidence_artifacts`` table). The retention period is
    # configurable per session, but for the test fixture a fixed
    # 30-day offset is self-documenting and never before the
    # capture regardless of the test date.
    retention_expires = capture_started + timedelta(days=30)
    first = EvidenceArtifact(
        flag_id=flag.id,
        kind=EvidenceKind.CLIP,
        storage_uri=f"s3://bucket/evidence/{flag.id}/a.webm",
        content_sha256="a" * 64,
        media_type="video/webm",
        byte_size=1024,
        capture_started_at=capture_started,
        retention_expires_at=retention_expires,
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
        retention_expires_at=retention_expires,
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


# ----------------------------------------------------------------------
# AdminUser: unique constraint (database-level)
# ----------------------------------------------------------------------


def test_admin_users_unique_constraint_enforced(
    integration_engine_session: Session,
) -> None:
    """The uq_admin_users_lms_identity constraint is enforced at SQL level."""

    admin = AdminUser(
        lti_issuer="https://lms.example.edu",
        lms_user_reference="admin-unique-test",
        role=AdminRole.ADMIN,
    )
    integration_engine_session.add(admin)
    integration_engine_session.commit()

    with pytest.raises(IntegrityError, match="uq_admin_users_lms_identity"):
        integration_engine_session.execute(
            text(
                "INSERT INTO admin_users (id, lti_issuer, lms_user_reference, role) "
                "VALUES (:id, :issuer, :ref, 'admin')"
            ),
            {
                "id": str(uuid.uuid4()),
                "issuer": "https://lms.example.edu",
                "ref": "admin-unique-test",
            },
        )
    integration_engine_session.rollback()


# ----------------------------------------------------------------------
# AdminUser: ON DELETE RESTRICT protects referencing rows
# ----------------------------------------------------------------------


def test_admin_user_fk_restrict_on_delete(
    integration_engine_session: Session,
) -> None:
    """Deleting an AdminUser referenced by a ProctorReview is blocked."""

    admin = AdminUser(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=f"admin-restrict-{uuid.uuid4()}",
        role=AdminRole.PROCTOR,
    )
    integration_engine_session.add(admin)
    integration_engine_session.flush()

    flag = _seed_flag(integration_engine_session)
    review = ProctorReview(
        flag_id=flag.id,
        reviewer_reference="proctor@example.edu",
        reviewer_admin_id=admin.id,
        decision=ReviewDecision.UPHELD,
    )
    integration_engine_session.add(review)
    integration_engine_session.commit()

    with pytest.raises((IntegrityError, ProgrammingError)):
        integration_engine_session.execute(
            text("DELETE FROM admin_users WHERE id = :id"),
            {"id": admin.id},
        )
    integration_engine_session.rollback()


# ----------------------------------------------------------------------
# AdminUser: admin_role enum values
# ----------------------------------------------------------------------


def test_admin_role_enum_values(
    integration_engine_session: Session,
) -> None:
    """The admin_role enum contains the expected values."""

    result = integration_engine_session.execute(
        text(
            "SELECT unnest(enum_range(NULL::admin_role))::text AS value "
            "ORDER BY value"
        )
    )
    values = [row[0] for row in result]
    assert "instructor" in values
    assert "admin" in values
    assert "proctor" in values


# ----------------------------------------------------------------------
# PolicyConfig.name: partial unique index (item 3)
# ----------------------------------------------------------------------


def test_policy_configs_partial_unique_index_exists(
    integration_engine_session: Session,
) -> None:
    """The partial unique index ``uq_policy_configs_active_name`` is
    installed by the initial migration with the expected ``WHERE``
    clause.

    Anchors the index definition so future migrations that try to add a
    plain ``unique=True`` on ``name`` will be caught by
    ``test_no_duplicate_schema_objects_in_post_initial_migrations``.
    """

    result = integration_engine_session.execute(
        text(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'policy_configs'
              AND indexname = 'uq_policy_configs_active_name'
            """
        )
    ).first()
    assert result is not None, (
        "Partial unique index 'uq_policy_configs_active_name' is missing "
        "from policy_configs."
    )
    indexdef = result[0].lower()
    assert "unique" in indexdef, f"Index is not unique: {indexdef!r}"
    assert "where" in indexdef, (
        f"Index is not partial — expected a WHERE clause in {indexdef!r}"
    )
    assert "is_active" in indexdef
    assert "retired_at" in indexdef


def test_policy_configs_multiple_retired_with_same_name_allowed(
    integration_engine_session: Session,
) -> None:
    """Multiple retired policies can share a ``name`` — only the
    active-and-non-retired slot is unique.

    The partial-unique-index replacement for ``unique=True`` exists
    specifically to support this: a policy can be retired and a new
    version can take its name without colliding on the unique index.
    """

    shared_name = f"versioned-policy-{uuid.uuid4()}"
    retired_v1 = PolicyConfig(
        name=shared_name,
        is_active=False,
        retired_at=utc_now() - timedelta(days=30),
    )
    retired_v2 = PolicyConfig(
        name=shared_name,
        is_active=False,
        retired_at=utc_now() - timedelta(days=15),
    )
    active_v3 = PolicyConfig(
        name=shared_name,
        is_active=True,
        retired_at=None,
    )
    integration_engine_session.add_all([retired_v1, retired_v2, active_v3])
    integration_engine_session.commit()  # No IntegrityError

    rows = (
        integration_engine_session.query(PolicyConfig)
        .filter(PolicyConfig.name == shared_name)
        .all()
    )
    assert len(rows) == 3


def test_policy_configs_two_active_same_name_rejected(
    integration_engine_session: Session,
) -> None:
    """The partial index still rejects two active, non-retired policies
    with the same name — the headline invariant the new constraint
    must preserve.
    """

    shared_name = f"duplicate-active-{uuid.uuid4()}"
    policy_a = PolicyConfig(name=shared_name)
    integration_engine_session.add(policy_a)
    integration_engine_session.flush()

    policy_b = PolicyConfig(name=shared_name)
    integration_engine_session.add(policy_b)
    with pytest.raises(IntegrityError):
        integration_engine_session.commit()
    integration_engine_session.rollback()


def test_policy_configs_retired_then_active_replaces_slot(
    integration_engine_session: Session,
) -> None:
    """After retiring the active policy, a new active policy can take
    its name.  This is the versioning workflow the partial index
    enables.
    """

    shared_name = f"versioned-{uuid.uuid4()}"
    policy_a = PolicyConfig(name=shared_name)
    integration_engine_session.add(policy_a)
    integration_engine_session.flush()

    # Retire the original
    policy_a.is_active = False
    policy_a.retired_at = utc_now()
    integration_engine_session.commit()

    # A new active policy with the same name should be accepted
    policy_b = PolicyConfig(name=shared_name)
    integration_engine_session.add(policy_b)
    integration_engine_session.commit()  # No IntegrityError
