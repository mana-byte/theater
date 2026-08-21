"""Add the usage table for per-turn token and cost tracking.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("participant_id", sa.Text(), nullable=False),
        sa.Column("tree_root_id", sa.Text(), nullable=True),
        sa.Column("usage_key", sa.Text(), nullable=True),
        sa.Column("ts", sa.REAL(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "cache_creation_input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "cache_read_input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "reasoning_output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("cost_microcents", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index("idx_usage_participant", "usage", ["participant_id", "ts"], unique=False)
    op.create_index("idx_usage_tree", "usage", ["tree_root_id", "ts"], unique=False)
    op.create_index("idx_usage_identity", "usage", ["participant_id", "usage_key"], unique=True)


def downgrade() -> None:
    op.drop_index("idx_usage_identity", table_name="usage")
    op.drop_index("idx_usage_tree", table_name="usage")
    op.drop_index("idx_usage_participant", table_name="usage")
    op.drop_table("usage")
