"""Make ``TerminationRecord.triggering_flag_id`` nullable.

Down-revision: ``20260718_0002`` (audit reconciliation)

Reason
------
The identity-verification handshake can fail at WebSocket connect time
when the ``face_recognition`` library is not installed AND no valid
``IdentityVerificationOverrideRequest`` exists.  In that case the
session is terminated immediately, *before* any flag has been raised.
The ``TerminationRecord`` row carries ``reason =
'identity_backend_unavailable_no_override'`` and no flag reference.

This migration:

1. Drops ``NOT NULL`` on ``termination_records.triggering_flag_id``.
2. Adds a CHECK constraint that ties the column's nullability to the
   specific reason:

   - ``reason = 'identity_backend_unavailable_no_override'`` ⟹
     ``triggering_flag_id IS NULL``
   - any other reason ⟹ ``triggering_flag_id IS NOT NULL``

   So the existing rule "every non‑identity-backend-unavailable
   termination has a flag" is preserved for every legacy path; only
   the new no‑override path is allowed to leave the column null.

The constraint is enforced at the database level so a future
service-layer regression that writes a row with a wrong
``reason``/``triggering_flag_id`` combination is rejected at insert
time, not silently accepted.
"""

from alembic import op


revision = "20260823_0003"
down_revision = "20260718_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop NOT NULL on the column.  This is a real, necessary DDL
    #    change: ``create_all`` cannot retroactively alter an existing
    #    column's nullability on a database that already ran the
    #    initial migration, which is the exact scenario this
    #    migration exists to fix.
    op.execute(
        "ALTER TABLE termination_records "
        "ALTER COLUMN triggering_flag_id DROP NOT NULL"
    )

    # 2. Add the conditional CHECK constraint.
    op.execute(
        """
        ALTER TABLE termination_records
        ADD CONSTRAINT ck_termination_flag_null_only_for_identity_backend_unavailable
        CHECK (
            (reason = 'identity_backend_unavailable_no_override'
             AND triggering_flag_id IS NULL)
            OR
            (reason <> 'identity_backend_unavailable_no_override'
             AND triggering_flag_id IS NOT NULL)
        )
        """
    )


def downgrade() -> None:
    # Drop the constraint first so re-applying NOT NULL doesn't fail
    # on the rows the new constraint allowed (if any).
    op.execute(
        "ALTER TABLE termination_records "
        "DROP CONSTRAINT IF EXISTS "
        "ck_termination_flag_null_only_for_identity_backend_unavailable"
    )

    op.execute(
        "ALTER TABLE termination_records "
        "ALTER COLUMN triggering_flag_id SET NOT NULL"
    )