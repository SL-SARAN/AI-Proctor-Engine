"""PostgreSQL Integration Test for Migration 20260718_0002 (flag_immutable trigger).

This test proves migration 20260718_0002 against a REAL PostgreSQL 15+ database
(configured via ``INTEGRATION_DATABASE_URL``).

Exact sequence executed:
1. Apply migrations up to ``20260717_0001``.
2. Insert a populated ``Flag`` row with real, valid data.
3. Apply migration ``20260718_0002``.
4. Attempt a raw SQL ``UPDATE`` against that pre-existing row directly (bypassing ORM)
   and confirm PostgreSQL rejects it via the ``prevent_flag_mutation()`` trigger.
5. Confirm the pre-existing row is untouched and readable.
6. Attempt a raw SQL ``DELETE`` against that row directly and confirm PostgreSQL rejects it.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from psycopg.errors import RaiseException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from proctoring_engine.models import (
    AdminRole,
    AdminUser,
    ExamSession,
    Flag,
    FlagSeverity,
    FlagStatus,
    Participant,
    PolicyConfig,
    SessionStatus,
)
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


def test_flag_immutable_trigger_migration_on_populated_postgres() -> None:
    url = _get_integration_url()
    config = _get_alembic_config(url)

    # Step 1: Clean database and apply initial schema
    _reset_database(url)
    command.upgrade(config, "20260717_0001")

    engine = create_engine(url)

    # Step 2: Seed populated Flag row with real valid data
    with Session(engine) as db:
        admin = AdminUser(
            id=uuid.uuid4(),
            lti_issuer="https://lms.example.edu",
            lms_user_reference="admin-1",
            display_name="Test Admin",
            department="Computer Science",
            role=AdminRole.HEAD,
        )
        participant = Participant(
            id=uuid.uuid4(),
            lti_issuer="https://lms.example.edu",
            lms_user_reference="student-101",
            display_name="Test Student",
        )
        policy = PolicyConfig(
            id=uuid.uuid4(),
            name="Default Security Policy",
            is_active=True,
        )
        db.add_all([admin, participant, policy])
        db.flush()

        exam_session = ExamSession(
            id=uuid.uuid4(),
            participant_id=participant.id,
            policy_config_id=policy.id,
            lti_issuer=participant.lti_issuer,
            lti_context_id="course-101",
            exam_reference="exam-midterm",
            attempt_reference="attempt-1",
            status=SessionStatus.PENDING,
        )
        db.add(exam_session)
        db.flush()

        flag = Flag(
            id=uuid.uuid4(),
            exam_session_id=exam_session.id,
            policy_config_id=policy.id,
            rule_code="SECOND_PERSON_DETECTED",
            severity=FlagSeverity.CRITICAL,
            status=FlagStatus.RAISED,
            confidence_score=0.95,
            confidence_lower=0.90,
            confidence_upper=0.99,
        )
        db.add(flag)
        db.commit()
        pre_existing_flag_id = flag.id

    # Step 3: Upgrade to migration 20260718_0002
    command.upgrade(config, "20260718_0002")

    # Step 4: Attempt raw SQL UPDATE against pre-existing row; assert trigger rejects it
    with pytest.raises(ProgrammingError) as exc_info:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE flags SET status = 'confirmed' WHERE id = :id"),
                {"id": pre_existing_flag_id},
            )

    orig_cause = exc_info.value.orig
    assert isinstance(orig_cause, RaiseException)
    assert "flag records are immutable" in str(exc_info.value)

    # Step 5: Confirm pre-existing row is untouched and readable
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, status, rule_code FROM flags WHERE id = :id"),
            {"id": pre_existing_flag_id},
        ).fetchone()

        assert row is not None
        assert row[0] == pre_existing_flag_id
        assert row[1] == "raised"  # Postgres enum string value
        assert row[2] == "SECOND_PERSON_DETECTED"

    # Step 6: Attempt raw SQL DELETE against pre-existing row; assert trigger rejects it
    with pytest.raises(ProgrammingError) as exc_info_del:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM flags WHERE id = :id"),
                {"id": pre_existing_flag_id},
            )

    orig_cause_del = exc_info_del.value.orig
    assert isinstance(orig_cause_del, RaiseException)
    assert "flag records are immutable" in str(exc_info_del.value)
