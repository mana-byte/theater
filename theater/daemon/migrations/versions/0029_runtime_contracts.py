"""Runtime wiring storage: bindings, control operations, terminal evidence.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "participant_runtime_bindings",
        sa.Column("participant_id", sa.Text(), primary_key=True),
        sa.Column("harness", sa.Text(), nullable=False),
        sa.Column("wiring", sa.Text(), nullable=False),
        sa.Column("backend_generation", sa.Integer(), nullable=False),
        sa.Column("lifecycle_phase", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("backend_pid", sa.Integer(), nullable=True),
        sa.Column("backend_started_at", sa.REAL(), nullable=True),
        sa.Column("native_session_id", sa.Text(), nullable=True),
        sa.Column("protocol", sa.Text(), nullable=True),
        sa.Column("protocol_version", sa.Text(), nullable=True),
        sa.Column("native_version", sa.Text(), nullable=True),
        sa.Column("compatibility_policy", sa.Text(), nullable=True),
        sa.Column("launch_policy", sa.Text(), nullable=True),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
    )
    op.create_index(
        "idx_runtime_bindings_session",
        "participant_runtime_bindings",
        ["native_session_id"],
    )

    op.create_table(
        "control_operations",
        sa.Column("operation_id", sa.Text(), primary_key=True),
        sa.Column("participant_id", sa.Text(), nullable=False),
        sa.Column("job_handle", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("transport", sa.Text(), nullable=False),
        sa.Column("delivery_phase", sa.Text(), nullable=False),
        sa.Column("delivery_result", sa.Text(), nullable=True),
        sa.Column("backend_generation", sa.Integer(), nullable=True),
        sa.Column("native_session_id", sa.Text(), nullable=True),
        sa.Column("native_turn_id", sa.Text(), nullable=True),
        sa.Column("queue_sequence", sa.Integer(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
    )
    op.create_index(
        "idx_control_operations_participant_phase",
        "control_operations",
        ["participant_id", "delivery_phase"],
    )
    op.create_index("idx_control_operations_job", "control_operations", ["job_handle"])
    op.create_index(
        "idx_control_operations_queue",
        "control_operations",
        ["participant_id", "queue_sequence"],
    )

    op.create_table(
        "native_terminal_evidence",
        sa.Column("participant_id", sa.Text(), primary_key=True),
        sa.Column("backend_generation", sa.Integer(), primary_key=True),
        sa.Column("native_session_id", sa.Text(), primary_key=True),
        sa.Column("native_turn_id", sa.Text(), primary_key=True),
        sa.Column("terminal", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("result_completeness", sa.Text(), nullable=False),
        sa.Column("result_provenance", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.REAL(), nullable=False),
    )
    op.create_index(
        "idx_native_terminal_evidence_participant",
        "native_terminal_evidence",
        ["participant_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_native_terminal_evidence_participant", table_name="native_terminal_evidence")
    op.drop_table("native_terminal_evidence")
    op.drop_index("idx_control_operations_queue", table_name="control_operations")
    op.drop_index("idx_control_operations_job", table_name="control_operations")
    op.drop_index("idx_control_operations_participant_phase", table_name="control_operations")
    op.drop_table("control_operations")
    op.drop_index("idx_runtime_bindings_session", table_name="participant_runtime_bindings")
    op.drop_table("participant_runtime_bindings")
