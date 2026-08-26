"""PostgreSQL Integration Test for Migration 20260823_0003.

This test proves migration 20260823_0003 against a REAL PostgreSQL 15+ database
(configured via ``INTEGRATION_DATABASE_URL``).

Exact sequence executed:
1. Apply migrations up to ``20260718_0002``.
2. Insert a ``termination_records`` row with ``reason='second_person'`` and a
   non-null ``triggering_flag_id`` (pre-existing production legacy data).
3. Apply migration ``20260823_0003``.
4. Assert against the live PostgreSQL database:
   - ``triggering_flag_id`` column is now nullable in ``information_schema.columns``.
   - Pre-existing legacy row is untouched and data is intact.
   - CHECK constraint ``ck_termination_flag_null_only_for_identity_backend_unavailable``
     exists in ``pg_constraint``.
   - Fresh raw SQL insert attempting ``reason='second_person'`` with
     ``triggering_flag_id=NULL`` is rejected by PostgreSQL (db-level IntegrityError).
   - Fresh raw SQL insert with ``reason='identity_backend_unavailable_no_override'``
     and ``triggering_flag_id=NULL`` succeeds.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

# pyrefly: ignore [missing-import]
from tests.integration.conftest import _is_postgres, _reset_database


def _get_integration_url() -> str:
    url = os.environ.get("INTEGRATION_DATABASE_URL")
    if not url:
        pytest.skip("INTEGRATION_DATABASE_URL is not set")
    if not _is_postgres(url):
        pytest.skip("INTEGRATION_DATABASE_URL is not a PostgreSQL URL")
    return url


def _get_alembic_config(url: str) -> Config:
    os.environ["DATABASE_URL"] = url
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    config = Config(os.path.join(root, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(root, "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("prepend_sys_path", f"{root}{os.pathsep}{root}/src")
    return config


def test_termination_flag_nullable_migration_on_populated_postgres() -> None:
    url = _get_integration_url()
    config = _get_alembic_config(url)

    # 1. Reset database to clean state
    _reset_database(url)

    # 2. Apply migrations up to 20260718_0002
    command.upgrade(config, "20260718_0002")

    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        # Generate UUIDs for pre-existing production data
        admin_id = uuid.uuid4()
        participant_id = uuid.uuid4()
        policy_id = uuid.uuid4()
        session_id = uuid.uuid4()
        flag_id = uuid.uuid4()
        legacy_termination_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        # Seed pre-existing legacy production data at revision 20260718_0002
        from proctoring_engine.models import (
            AdminRole,
            AdminUser,
            DeliveryStatus,
            ExamSession,
            Flag,
            FlagSeverity,
            FlagStatus,
            Participant,
            PolicyConfig,
            SessionStatus,
            TerminationRecord,
        )
        from sqlalchemy.orm import Session

        with Session(engine) as db:
            admin = AdminUser(
                id=admin_id,
                lti_issuer="https://lms.example.edu",
                lms_user_reference="admin-1",
                display_name="Test Admin",
                department="CS",
                role=AdminRole.HEAD,
            )
            participant = Participant(
                id=participant_id,
                lti_issuer="https://lms.example.edu",
                lms_user_reference="student-1",
                display_name="Test Student",
            )
            policy = PolicyConfig(
                id=policy_id,
                name="default-policy",
                is_active=True,
            )
            db.add_all([admin, participant, policy])
            db.flush()

            session = ExamSession(
                id=session_id,
                participant_id=participant.id,
                policy_config_id=policy.id,
                lti_issuer="https://lms.example.edu",
                lti_context_id="course-1",
                exam_reference="exam-1",
                attempt_reference="attempt-1",
                status=SessionStatus.PENDING,
            )
            db.add(session)
            db.flush()

            flag = Flag(
                id=flag_id,
                exam_session_id=session.id,
                policy_config_id=policy.id,
                rule_code="FLAG_SECOND_PERSON",
                severity=FlagSeverity.HIGH,
                status=FlagStatus.RAISED,
                confidence_score=0.9,
                confidence_lower=0.8,
                confidence_upper=1.0,
            )
            db.add(flag)
            db.flush()

            legacy_tr = TerminationRecord(
                id=legacy_termination_id,
                exam_session_id=session.id,
                triggering_flag_id=flag.id,
                reason="second_person",
                client_delivery_status=DeliveryStatus.SENT,
                lms_delivery_status=DeliveryStatus.SENT,
            )
            db.add(legacy_tr)
            db.commit()

        # 3. Apply migration 20260823_0003 against populated database
        command.upgrade(config, "20260823_0003")

        # 4. Assertions against live PostgreSQL instance
        with engine.connect() as conn:
            # Assertion A: Column is now nullable in Postgres catalog
            col_res = conn.execute(
                text(
                    """
                    SELECT is_nullable FROM information_schema.columns
                    WHERE table_name = 'termination_records' AND column_name = 'triggering_flag_id'
                    """
                )
            ).scalar_one()
            assert col_res == "YES", f"Expected triggering_flag_id is_nullable to be 'YES', got {col_res!r}"

            # Assertion B: Pre-existing row from Step 2 is untouched & intact
            row = conn.execute(
                text(
                    """
                    SELECT id, triggering_flag_id, reason FROM termination_records
                    WHERE id = :id
                    """
                ),
                {"id": legacy_termination_id},
            ).mappings().one()
            assert row["id"] == legacy_termination_id
            assert row["triggering_flag_id"] == flag_id
            assert row["reason"] == "second_person"

            # Assertion C: CHECK constraint exists in schema (pg_constraint)
            chk_res = conn.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conname = 'ck_termination_flag_null_only_for_identity_backend_unavailable'
                    """
                )
            ).scalar()
            assert chk_res == "ck_termination_flag_null_only_for_identity_backend_unavailable"

            # Assertion D: Fresh raw SQL insert with reason='second_person' & triggering_flag_id=NULL is REJECTED by Postgres engine
            session_id_2 = uuid.uuid4()
            conn.execute(
                text(
                    """
                    INSERT INTO exam_sessions (
                        id, participant_id, policy_config_id, lti_issuer, lti_context_id, exam_reference,
                        attempt_reference, status, allowed_reference_materials, permitted_material_details,
                        accumulated_medium_score, identity_verification_status, created_at
                    ) VALUES (
                        :id, :p_id, :pol_id, 'https://lms.example.edu', 'course-1', 'exam-1',
                        :attempt_ref, 'pending', 'closed_book', '{}',
                        0, 'pending_check', :now
                    )
                    """
                ),
                {"id": session_id_2, "p_id": participant_id, "pol_id": policy_id, "attempt_ref": "attempt-2", "now": now},
            )

            illegal_id = uuid.uuid4()
            with pytest.raises(IntegrityError) as exc_info:
                with conn.begin_nested():
                    conn.execute(
                        text(
                            """
                            INSERT INTO termination_records (id, exam_session_id, triggering_flag_id, reason, client_delivery_status, lms_delivery_status, created_at)
                            VALUES (:id, :s_id, NULL, 'second_person', 'sent', 'sent', :now)
                            """
                        ),
                        {"id": illegal_id, "s_id": session_id_2, "now": now},
                    )
            assert "ck_termination_flag_null_only_for_identity_backend_unavailable" in str(exc_info.value)

            # Assertion E: Fresh raw SQL insert with reason='identity_backend_unavailable_no_override' & triggering_flag_id=NULL SUCCEEDS
            session_id_3 = uuid.uuid4()
            conn.execute(
                text(
                    """
                    INSERT INTO exam_sessions (
                        id, participant_id, policy_config_id, lti_issuer, lti_context_id, exam_reference,
                        attempt_reference, status, allowed_reference_materials, permitted_material_details,
                        accumulated_medium_score, identity_verification_status, created_at
                    ) VALUES (
                        :id, :p_id, :pol_id, 'https://lms.example.edu', 'course-1', 'exam-1',
                        :attempt_ref, 'pending', 'closed_book', '{}',
                        0, 'pending_check', :now
                    )
                    """
                ),
                {"id": session_id_3, "p_id": participant_id, "pol_id": policy_id, "attempt_ref": "attempt-3", "now": now},
            )

            valid_id = uuid.uuid4()
            conn.execute(
                text(
                    """
                    INSERT INTO termination_records (id, exam_session_id, triggering_flag_id, reason, client_delivery_status, lms_delivery_status, created_at)
                    VALUES (:id, :s_id, NULL, 'identity_backend_unavailable_no_override', 'sent', 'sent', :now)
                    """
                ),
                {"id": valid_id, "s_id": session_id_3, "now": now},
            )

            inserted_row = conn.execute(
                text(
                    """
                    SELECT id, triggering_flag_id, reason FROM termination_records
                    WHERE id = :id
                    """
                ),
                {"id": valid_id},
            ).mappings().one()
            assert inserted_row["id"] == valid_id
            assert inserted_row["triggering_flag_id"] is None
            assert inserted_row["reason"] == "identity_backend_unavailable_no_override"

    finally:
        engine.dispose()
