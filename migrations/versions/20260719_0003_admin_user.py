"""AdminUser table and admin-identity FK columns.

Revision ID: 20260719_0003
Revises: 20260718_0002
Create Date: 2026-07-19

This migration resolves the open decision documented in
``docs/01-data-models-design.md`` §"Open decision: admin / reviewer identity"
by adding a dedicated ``admin_users`` table and wiring FK columns from the
three tables that previously stored admin references as free-form strings:

* ``policy_configs.created_by_id`` → ``admin_users.id``
* ``accommodation_exemptions.approved_by_admin_id`` → ``admin_users.id``
* ``proctor_reviews.reviewer_admin_id`` → ``admin_users.id``

All three FK columns are nullable because:

1. The system is pre-production with no data to backfill.
2. Existing test data uses string-only references that have no corresponding
   ``AdminUser`` row.
3. The service layer (a future layer) will enforce that new writes always
   populate the FK.

The original string columns (``approved_by``, ``reviewer_reference``) are
preserved for backward compatibility. ``PolicyConfig`` did not previously
have a ``created_by`` column in the DDL — the design doc listed it, but it
was never emitted. This migration adds the FK column directly; the string
counterpart is not needed since no data exists to preserve.

All FKs use ``ON DELETE RESTRICT`` so a retired admin is preserved for
audit, not cascaded away.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260719_0003"
down_revision = "20260718_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the admin_users table and add FK columns to referencing tables."""

    bind = op.get_bind()

    # ------------------------------------------------------------------
    # 1. admin_role enum (PostgreSQL only; SQLite uses VARCHAR checks).
    # ------------------------------------------------------------------
    if bind.dialect.name == "postgresql":
        op.execute("CREATE TYPE admin_role AS ENUM ('instructor', 'admin', 'proctor')")

    # ------------------------------------------------------------------
    # 2. admin_users table.
    # ------------------------------------------------------------------
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("lti_issuer", sa.String(512), nullable=False),
        sa.Column("lms_user_reference", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(512), nullable=True),
        sa.Column(
            "role",
            sa.Enum(
                "instructor",
                "admin",
                "proctor",
                name="admin_role",
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "lti_issuer",
            "lms_user_reference",
            name="uq_admin_users_lms_identity",
        ),
    )

    # ------------------------------------------------------------------
    # 3. policy_configs.created_by_id — new FK column.
    # ------------------------------------------------------------------
    op.add_column(
        "policy_configs",
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_policy_configs_created_by",
        "policy_configs",
        "admin_users",
        ["created_by_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # ------------------------------------------------------------------
    # 4. accommodation_exemptions.approved_by_admin_id — new FK column.
    # ------------------------------------------------------------------
    op.add_column(
        "accommodation_exemptions",
        sa.Column("approved_by_admin_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_accommodation_exemptions_approved_by_admin",
        "accommodation_exemptions",
        "admin_users",
        ["approved_by_admin_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # ------------------------------------------------------------------
    # 5. proctor_reviews.reviewer_admin_id — new FK column.
    # ------------------------------------------------------------------
    op.add_column(
        "proctor_reviews",
        sa.Column("reviewer_admin_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_proctor_reviews_reviewer_admin",
        "proctor_reviews",
        "admin_users",
        ["reviewer_admin_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Remove the admin_users table and the FK columns from referencing tables."""

    bind = op.get_bind()

    # Drop FK columns in reverse order.
    op.drop_constraint(
        "fk_proctor_reviews_reviewer_admin", "proctor_reviews", type_="foreignkey"
    )
    op.drop_column("proctor_reviews", "reviewer_admin_id")

    op.drop_constraint(
        "fk_accommodation_exemptions_approved_by_admin",
        "accommodation_exemptions",
        type_="foreignkey",
    )
    op.drop_column("accommodation_exemptions", "approved_by_admin_id")

    op.drop_constraint(
        "fk_policy_configs_created_by", "policy_configs", type_="foreignkey"
    )
    op.drop_column("policy_configs", "created_by_id")

    # Drop the table.
    op.drop_table("admin_users")

    # Drop the enum type (Postgres only).
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS admin_role")
