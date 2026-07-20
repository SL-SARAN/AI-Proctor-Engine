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
* The initial migration creates the ``session_status``, ``flag_status``,
  and ``review_decision`` enum types from the Python enums in
  ``src/proctoring_engine/models.py`` (``Base.metadata.create_all``).
  Those Python enums already include the values added by the audit
  reconciliation (``under_review``, ``overturned``, ``needs_more_info``),
  so no ``ALTER TYPE ... ADD VALUE`` statements are needed here — the
  values are present from the initial migration onward.

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
    # 1. session_status enum: ``under_review`` is already present.
    #
    #    The initial migration creates the schema from the Python
    #    ``SessionStatus`` enum (``Base.metadata.create_all``), which
    #    already includes ``UNDER_REVIEW = "under_review"``. So by the
    #    time this migration runs, the PostgreSQL ``session_status``
    #    type already has ``under_review`` as a label, and an
    #    ``ALTER TYPE ... ADD VALUE 'under_review'`` would fail with
    #    ``DuplicateObject``.
    #
    #    The same is true of ``flag_status.overturned`` and
    #    ``review_decision.needs_more_info`` — the Python enums in
    #    ``src/proctoring_engine/models.py`` are the single source of
    #    truth for the type labels, and they already include the values
    #    this migration was originally written to add. Nothing needs
    #    to be done here.
    #
    #    Note: there is no ``cancelled`` label in the Python enum and
    #    never was; no rename is needed. ``pending`` is the first
    #    value of the Python enum, which is what the initial migration
    #    emits.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 2. policy_configs: new column + new check constraints.
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
    # 3. exam_sessions: new column + new check constraint.
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
    # 4. enrollment_references: new column.
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
    # 5. flags: two new columns + a new FK to accommodation_exemptions.
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
    # 6. evidence_artifacts: unique constraint on flag_id.
    # ------------------------------------------------------------------
    op.create_unique_constraint(
        "uq_evidence_artifacts_one_per_flag",
        "evidence_artifacts",
        ["flag_id"],
    )

    # ------------------------------------------------------------------
    # 7. flag_immutable trigger (Postgres mirror of the ORM-level
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

    Downgrade drops the new trigger, columns, and constraints in reverse
    order. The enum labels (``session_status``, ``flag_status``,
    ``review_decision``) are not modified by this migration: the initial
    migration creates the enum types from the Python enums in
    ``src/proctoring_engine/models.py``, so reversing this migration does
    not change which values the enums contain. To change the enum
    membership, update the Python enums and write a new migration —
    ``ALTER TYPE ... DROP VALUE`` is not supported in Postgres 15, so any
    removal would require a new migration that recreates the type.
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

    # The enum types (``session_status``, ``flag_status``,
    # ``review_decision``) are owned by the initial migration's
    # ``Base.metadata.create_all``. This migration did not add any enum
    # values, so there is nothing to drop here. The downgrade is partial
    # by design: only the changes made by this migration are reversed.
    # To remove an enum value, write a new migration that recreates the
    # type — ``ALTER TYPE ... DROP VALUE`` is not supported in Postgres
    # 15.
