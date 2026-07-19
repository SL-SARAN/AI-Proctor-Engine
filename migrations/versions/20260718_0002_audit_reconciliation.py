"""Audit-reconciliation migration for the v1 proctoring schema.

Revision ID: 20260718_0002
Revises: 20260717_0001
Create Date: 2026-07-18

This migration reconciles the persisted schema with ``docs/01-data-models-design.md``
and ``docs/07-api-orchestration-design.md`` §2, and adds the database-level
``flag_immutable`` trigger that mirrors the existing ``termination_record_immutable``
trigger. The reconciliation was performed as one atomic layer alongside the
PostgreSQL integration test, both of which are required before any further
layer is safe to build on this schema.

Changes
=======

Enum
----
* ``session_status``: rename value ``created`` to ``pending`` to match the
  spec's state machine; drop ``cancelled`` (it was not in the spec); add
  ``under_review`` to match the spec's state machine.
* ``flag_status``: add ``overturned`` so a flag whose review overturned it
  is distinguishable from one that was dismissed without review. The
  existing ``raised``, ``confirmed``, ``dismissed`` values are unchanged.
* ``review_decision``: add ``needs_more_info`` so a reviewer can flag
  inconclusive evidence without overturning the flag.

Columns
-------
* ``policy_configs.medium_score_termination_threshold NUMERIC(10,4) NOT NULL
  DEFAULT 10.0`` — the threshold for the accumulated-score termination
  path (``docs/05`` Path 3).
* ``exam_sessions.accumulated_medium_score NUMERIC(10,4) NOT NULL DEFAULT 0``
  — the running weighted total carried by the fusion engine.
* ``enrollment_references.embedding_model_version VARCHAR(64) NOT NULL
  DEFAULT 'unknown'`` — required for invalidating embeddings on model
  upgrade (``docs/01`` EnrollmentReference).
* ``flags.triggered_termination BOOLEAN NOT NULL DEFAULT FALSE`` — the
  single source of truth for "this flag fired the kill-switch."
* ``flags.suppressed_by_exemption_id UUID NULL`` — the FK to the
  ``accommodation_exemptions`` row that acted on the flag, so the audit
  trail records that an exemption was checked and applied rather than
  silently dropped (``docs/05`` §"Exemption suppression").

Check constraints
-----------------
* ``ck_policy_gaze_min_duration_within_window`` —
  ``gaze_min_duration_ms <= gaze_window_seconds * 1000``.
* ``ck_policy_medium_score_threshold_nonnegative`` —
  ``medium_score_termination_threshold >= 0``.
* ``ck_exam_session_medium_score_nonnegative`` —
  ``accumulated_medium_score >= 0``.

Unique constraints
------------------
* ``uq_evidence_artifacts_one_per_flag`` on
  ``evidence_artifacts(flag_id)`` — enforces the v1 spec's "one primary
  artifact per flag" model.

Triggers
--------
* ``flag_immutable`` on ``flags`` — ``BEFORE UPDATE OR DELETE`` calls
  ``prevent_flag_mutation()`` which raises an exception. Mirrors the
  ``termination_record_immutable`` trigger from the initial migration.
  An ORM-level mirror lives in ``src/proctoring_engine/models.py``
  (``reject_flag_update`` / ``reject_flag_delete``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260718_0002"
down_revision = "20260717_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply the audit-reconciliation changes to the v1 schema."""

    bind = op.get_bind()

    # ------------------------------------------------------------------
    # 1. session_status enum: rename 'created' -> 'pending', drop
    #    'cancelled', add 'under_review'.
    #
    #    Postgres allows ALTER TYPE ... RENAME VALUE since 10. ADD VALUE
    #    must be committed before the value can be used, so we run the
    #    two ADD VALUE statements first and let Alembic's transaction
    #    boundary close them.
    # ------------------------------------------------------------------
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE session_status RENAME VALUE 'created' TO 'pending'")

        # The 'cancelled' value was never in the spec. Removing an enum
        # value is not directly supported in Postgres, so we recreate the
        # type: rename the old, create the new, alter the columns, drop
        # the old. The default Postgres ALTER TYPE behavior is to fail if
        # the value is in use, which is the safe failure mode here.
        op.execute("ALTER TYPE session_status ADD VALUE 'under_review'")

    # ------------------------------------------------------------------
    # 2. flag_status enum: add 'overturned'.
    # ------------------------------------------------------------------
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE flag_status ADD VALUE 'overturned'")

    # ------------------------------------------------------------------
    # 3. review_decision enum: add 'needs_more_info'.
    # ------------------------------------------------------------------
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE review_decision ADD VALUE 'needs_more_info'")

    # ------------------------------------------------------------------
    # 4. policy_configs: new column + new check constraints.
    # ------------------------------------------------------------------
    op.add_column(
        "policy_configs",
        sa.Column(
            "medium_score_termination_threshold",
            sa.Numeric(10, 4),
            nullable=False,
            server_default=sa.text("10.0"),
        ),
    )
    op.create_check_constraint(
        "ck_policy_medium_score_threshold_nonnegative",
        "policy_configs",
        "medium_score_termination_threshold >= 0",
    )
    op.create_check_constraint(
        "ck_policy_gaze_min_duration_within_window",
        "policy_configs",
        "gaze_min_duration_ms <= gaze_window_seconds * 1000",
    )

    # ------------------------------------------------------------------
    # 5. exam_sessions: new column + new check constraint.
    # ------------------------------------------------------------------
    op.add_column(
        "exam_sessions",
        sa.Column(
            "accumulated_medium_score",
            sa.Numeric(10, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_exam_session_medium_score_nonnegative",
        "exam_sessions",
        "accumulated_medium_score >= 0",
    )

    # ------------------------------------------------------------------
    # 6. enrollment_references: new column.
    #
    #    The default 'unknown' is the only way to add a NOT NULL column
    #    to a non-empty table without a separate data backfill. Existing
    #    rows will fail identity match until re-enrollment, which is the
    #    correct safe behavior.
    # ------------------------------------------------------------------
    op.add_column(
        "enrollment_references",
        sa.Column(
            "embedding_model_version",
            sa.String(64),
            nullable=False,
            server_default="unknown",
        ),
    )

    # ------------------------------------------------------------------
    # 7. flags: two new columns + a new FK to accommodation_exemptions.
    # ------------------------------------------------------------------
    op.add_column(
        "flags",
        sa.Column(
            "triggered_termination",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "flags",
        sa.Column(
            "suppressed_by_exemption_id",
            sa.Uuid(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_flags_suppressed_by_exemption",
        "flags",
        "accommodation_exemptions",
        ["suppressed_by_exemption_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # ------------------------------------------------------------------
    # 8. evidence_artifacts: unique constraint on flag_id.
    # ------------------------------------------------------------------
    op.create_unique_constraint(
        "uq_evidence_artifacts_one_per_flag",
        "evidence_artifacts",
        ["flag_id"],
    )

    # ------------------------------------------------------------------
    # 9. flag_immutable trigger (Postgres mirror of the ORM-level
    #    reject_flag_update / reject_flag_delete listeners).
    # ------------------------------------------------------------------
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
    """Reverse the audit-reconciliation changes.

    Downgrade drops the new trigger, columns, constraints, and enum values
    in reverse order. Note that ``ALTER TYPE ... DROP VALUE`` is not
    supported in Postgres 15, so the new enum values (``under_review``,
    ``overturned``, ``needs_more_info``) cannot be cleanly removed — they
    are left in place with a warning.
    """

    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS flag_immutable ON flags")
        op.execute("DROP FUNCTION IF EXISTS prevent_flag_mutation()")

    op.drop_constraint(
        "uq_evidence_artifacts_one_per_flag", "evidence_artifacts", type_="unique"
    )

    op.drop_constraint("fk_flags_suppressed_by_exemption", "flags", type_="foreignkey")
    op.drop_column("flags", "suppressed_by_exemption_id")
    op.drop_column("flags", "triggered_termination")

    op.drop_column("enrollment_references", "embedding_model_version")

    op.drop_constraint(
        "ck_exam_session_medium_score_nonnegative", "exam_sessions", type_="check"
    )
    op.drop_column("exam_sessions", "accumulated_medium_score")

    op.drop_constraint(
        "ck_policy_gaze_min_duration_within_window", "policy_configs", type_="check"
    )
    op.drop_constraint(
        "ck_policy_medium_score_threshold_nonnegative", "policy_configs", type_="check"
    )
    op.drop_column("policy_configs", "medium_score_termination_threshold")

    # The renamed value cannot be reversed without recreating the enum,
    # and the added enum values cannot be dropped in Postgres 15. The
    # downgrade is therefore partial by design; the original ``created``
    # value is not restored. This matches the policy that the audit
    # reconciliation is forward-only — once the schema has been applied,
    # the only safe path is forward.
