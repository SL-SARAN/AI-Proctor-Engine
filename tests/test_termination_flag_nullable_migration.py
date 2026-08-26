"""Unit test for the ``20260823_0003`` migration DDL generation and ORM model.

Scope & Intent (Unit Tier)
--------------------------
This file tests two specific unit-level properties:

1. **Alembic DDL String Generation**: Renders the PostgreSQL dialect DDL
   emitted by ``20260823_0003`` in dry-run mode (``as_sql=True``) to confirm
   it contains ``ALTER COLUMN ... DROP NOT NULL`` and the expected CHECK
   constraint predicate text.
2. **SQLAlchemy ORM Model Validation**: Confirms Python SQLAlchemy model
   syntax and validation for ``TerminationRecord(triggering_flag_id=None)``
   on an in-memory SQLite schema (verifying Python-side ORM semantics).

**What this test does NOT prove**:
This unit test does NOT execute the live SQL migration against a running
PostgreSQL database engine, nor does it test PostgreSQL's database-level
constraint enforcement on populated legacy rows.

**Live Migration Proof**:
The actual database-level migration execution, schema modification, legacy
data preservation, and PostgreSQL engine-level constraint enforcement are
proven by the real integration test in:
``tests/integration/test_termination_flag_nullable_postgres.py``
(run against PostgreSQL via ``INTEGRATION_DATABASE_URL``).
"""

from __future__ import annotations

import io
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _render_upgrade_sql(revision: str) -> str:
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR.parent))
    script = ScriptDirectory.from_config(config)
    rev = script.get_revision(revision)
    assert rev is not None, f"Revision {revision!r} not found"
    module = rev.module

    buffer = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer},
    )
    with Operations.context(ctx):
        module.upgrade()
    return buffer.getvalue()


def test_termination_flag_nullable_migration_makes_column_nullable() -> None:
    """The migration must issue ``ALTER COLUMN ... DROP NOT NULL``."""
    sql = _render_upgrade_sql("20260823_0003")
    assert "ALTER TABLE termination_records" in sql
    assert "ALTER COLUMN triggering_flag_id DROP NOT NULL" in sql


def test_termination_flag_nullable_migration_adds_check_constraint() -> None:
    """The migration must add a CHECK constraint tying nullability to reason."""
    sql = _render_upgrade_sql("20260823_0003")
    assert (
        "ck_termination_flag_null_only_for_identity_backend_unavailable"
        in sql
    )
    assert "ADD CONSTRAINT" in sql
    # The constraint predicate must reference the new reason value and
    # the triggering_flag_id column.
    assert "identity_backend_unavailable_no_override" in sql
    assert "triggering_flag_id IS NULL" in sql
    assert "triggering_flag_id IS NOT NULL" in sql


def test_termination_flag_nullable_migration_check_is_strict_for_other_reasons() -> None:
    """Other termination reasons must still require a non-null flag id.

    The constraint is the safety belt: if a future service-layer bug
    tries to insert a row with reason != 'identity_backend_unavailable_no_override'
    and triggering_flag_id IS NULL, the database rejects it.
    """
    sql = _render_upgrade_sql("20260823_0003")
    # The "other reason" branch of the constraint must require NOT NULL.
    assert "reason <> 'identity_backend_unavailable_no_override'" in sql or \
        "reason != 'identity_backend_unavailable_no_override'" in sql


def test_termination_flag_nullable_migration_referenced_in_chain() -> None:
    """The migration must be linked correctly in the chain.

    `down_revision` must point at the previous revision's identifier
    exactly (not its file name) — this guards against the typo bug
    that broke the chain on this turn's first iteration.
    """
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR.parent))
    script = ScriptDirectory.from_config(config)
    rev = script.get_revision("20260823_0003")
    assert rev is not None
    assert rev.down_revision == "20260718_0002"


def test_orm_model_declares_triggering_flag_id_nullable() -> None:
    """The ORM model must reflect the migration's intent.

    Once the migration runs, the ORM model must be able to construct
    a ``TerminationRecord`` with ``triggering_flag_id=None`` and a
    ``reason`` of the no-override value.  This is the Python-side
    counterpart to the database CHECK constraint — both have to agree
    or the application breaks.
    """
    from datetime import datetime, timezone
    import uuid as _uuid

    from proctoring_engine.models import (
        AdminRole,
        AdminUser,
        Base,
        DeliveryStatus,
        ExamSession,
        Participant,
        PolicyConfig,
        SessionStatus,
        TerminationRecord,
    )
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    with SessionLocal() as db:
        # Set up the minimum row set to satisfy FKs.
        admin = AdminUser(
            lti_issuer="https://lms.example.edu",
            lms_user_reference="admin-1",
            display_name="Test Admin",
            department="computer-science",
            role=AdminRole.HEAD,
        )
        participant = Participant(
            lti_issuer="https://lms.example.edu",
            lms_user_reference="student-1",
            display_name="Test Student",
        )
        policy = PolicyConfig(name="default-policy", is_active=True)
        db.add_all([admin, participant, policy])
        db.flush()

        session = ExamSession(
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

        # The ORM must permit triggering_flag_id=None with this reason.
        # Without the migration, this insert would fail because the
        # column is declared NOT NULL in the schema produced by
        # Base.metadata.create_all against the pre-migration model.
        tr = TerminationRecord(
            exam_session_id=session.id,
            triggering_flag_id=None,  # the whole point of the migration
            reason="identity_backend_unavailable_no_override",
            client_delivery_status=DeliveryStatus.SENT,
            lms_delivery_status=DeliveryStatus.SENT,
        )
        db.add(tr)
        db.commit()
        db.refresh(tr)

        assert tr.triggering_flag_id is None
        assert tr.reason == "identity_backend_unavailable_no_override"
        assert tr.exam_session_id == session.id