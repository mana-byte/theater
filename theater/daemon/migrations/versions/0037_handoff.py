"""Durable handoffs: job completion evidence and gated deferred tasks.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_evidence",
        sa.Column("job_handle", sa.Text(), primary_key=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("answer", sa.Text()),
        sa.Column("answer_chars", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("answer_source", sa.Text(), nullable=False, server_default=sa.text("'none'")),
        sa.Column("provenance", sa.Text()),
        sa.Column("harness", sa.Text()),
        sa.Column("participant_id", sa.Text()),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("cwd", sa.Text()),
        sa.Column("branch", sa.Text()),
        sa.Column("commit_sha", sa.Text()),
        sa.Column("dirty_entries", sa.Integer()),
        sa.Column("pin_state", sa.Text(), nullable=False),
        sa.Column("pin_reason", sa.Text()),
        sa.Column("captured_at", sa.REAL(), nullable=False),
    )
    op.create_table(
        "handoff_gates",
        sa.Column("job_handle", sa.Text(), primary_key=True),
        sa.Column("caller_id", sa.Text()),
        sa.Column("source_handle", sa.Text(), nullable=False),
        sa.Column("source_participant_id", sa.Text()),
        sa.Column("source_name", sa.Text()),
        sa.Column("task_kind", sa.Text(), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
    )
    op.create_index("idx_handoff_gates_source", "handoff_gates", ["state", "source_handle"])
    op.create_index("idx_handoff_gates_caller", "handoff_gates", ["caller_id", "state"])


def downgrade() -> None:
    op.drop_index("idx_handoff_gates_caller", table_name="handoff_gates")
    op.drop_index("idx_handoff_gates_source", table_name="handoff_gates")
    op.drop_table("handoff_gates")
    op.drop_table("job_evidence")
