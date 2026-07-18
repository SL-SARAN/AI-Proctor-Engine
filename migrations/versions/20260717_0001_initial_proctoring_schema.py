"""Create the v1 proctoring data-model schema.

Revision ID: 20260717_0001
Revises:
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op

from proctoring_engine.models import Base

revision = "20260717_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all model tables, indexes, constraints, and immutable-record trigger."""

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION prevent_termination_record_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'termination_records are immutable';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER termination_record_immutable
            BEFORE UPDATE OR DELETE ON termination_records
            FOR EACH ROW EXECUTE FUNCTION prevent_termination_record_mutation();
            """
        )


def downgrade() -> None:
    """Remove the v1 data-model objects in reverse dependency order."""

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS termination_record_immutable ON termination_records")
        op.execute("DROP FUNCTION IF EXISTS prevent_termination_record_mutation()")
    Base.metadata.drop_all(bind=bind)

