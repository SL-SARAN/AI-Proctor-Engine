"""Database-level ``flag_immutable`` trigger for the v1 proctoring schema.

Revision ID: 20260718_0002
Revises: 20260717_0001
Create Date: 2026-07-18

This migration originally performed an "audit reconciliation" of the schema
against ``docs/01-data-models-design.md`` and
``docs/07-api-orchestration-design.md`` §2 — adding enum values, columns,
check constraints, a unique constraint, and a foreign key. After the
audit-reconciliation commit ``09f0969`` landed, the initial migration
(``20260717_0001``) was the only place that needed to change for the
schema to match the spec: it already calls ``Base.metadata.create_all``,
which emits the columns, constraints, and enum types from the *post-*
audit-reconciliation ORM models in ``src/proctoring_engine/models.py``.

What is left for this migration
===============================

The one piece the initial migration cannot model: a PostgreSQL
``BEFORE UPDATE OR DELETE`` trigger on the ``flags`` table. The
corresponding ORM-level listeners (``reject_flag_update`` /
``reject_flag_delete`` in ``src/proctoring_engine/models.py``) are
already in place, but only fire on ORM-mediated writes. The trigger
catches direct SQL writes — ``psql`` sessions, future services that
bypass the ORM, or accidental raw-SQL writes from the application —
and mirrors the existing ``termination_record_immutable`` trigger that
the initial migration installs for ``termination_records``.

This is the defense-in-depth half of the immutability guarantee. The
ORM listener is the primary guard for application code; the trigger is
the backstop for everything else.

For the original full audit-reconciliation change list (enum values,
columns, constraints), see commit ``09f0969`` and the ORM model. None of
those operations are part of this migration's ``upgrade()`` body
because they are all already applied by the initial migration.
"""

from __future__ import annotations

from alembic import op


revision = "20260718_0002"
down_revision = "20260717_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Install the ``flag_immutable`` database trigger.

    The initial migration (``20260717_0001``) already installs
    ``termination_record_immutable`` and creates the schema from
    ``Base.metadata``. This migration only adds the parallel
    ``flag_immutable`` trigger that the initial migration's
    ``create_all`` cannot emit (SQLAlchemy does not model DML triggers).
    """

    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION prevent_flag_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'flag records are immutable';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            DROP TRIGGER IF EXISTS flag_immutable ON flags;
            CREATE TRIGGER flag_immutable
            BEFORE UPDATE OR DELETE ON flags
            FOR EACH ROW EXECUTE FUNCTION prevent_flag_mutation();
            """
        )


def downgrade() -> None:
    """Remove the ``flag_immutable`` database trigger and its function.

    Reversing this migration does not change the schema shape: the
    columns, check constraints, and enum values that this migration
    used to add are owned by the initial migration's
    ``Base.metadata.create_all``. The only thing this migration ever
    owned is the trigger.
    """

    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS flag_immutable ON flags")
        op.execute("DROP FUNCTION IF EXISTS prevent_flag_mutation()")
