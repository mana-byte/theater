"""Persist unresolved native prompt execution barriers.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "control_operations",
        sa.Column("execution_barrier", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    # Existing native prompt rows that may have crossed the wire were already
    # ambiguous before this release. Preserve their fail-closed recovery
    # obligation instead of treating the migration default as proof of idle.
    op.execute(
        """
        UPDATE control_operations
        SET execution_barrier = 1
        WHERE transport = 'native_runtime'
          AND kind IN ('send', 'queue_followup')
          AND (
              delivery_phase = 'dispatched'
              OR (delivery_phase = 'settled' AND delivery_result = 'unknown')
          )
        """
    )
    op.create_index(
        "idx_control_operations_execution_barrier",
        "control_operations",
        ["participant_id", "execution_barrier"],
    )


def downgrade() -> None:
    op.drop_index("idx_control_operations_execution_barrier", table_name="control_operations")
    op.drop_column("control_operations", "execution_barrier")
