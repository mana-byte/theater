"""Plays: started instances with pinned rules, held positions, and pending bindings.

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "play_instances",
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("master_id", sa.Text(), nullable=False),
        sa.Column("play", sa.Text(), nullable=False),
        sa.Column("rules", sa.Text(), nullable=False),
        sa.Column("rules_sha256", sa.Text(), nullable=False),
        sa.Column("created_at", sa.REAL(), nullable=False),
    )
    op.create_index("idx_play_instances_master", "play_instances", ["master_id"])
    op.create_table(
        "play_members",
        sa.Column("participant_id", sa.Text(), primary_key=True),
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column("created_at", sa.REAL(), nullable=False),
    )
    op.create_table(
        "play_assignments",
        sa.Column("job_handle", sa.Text(), primary_key=True),
        sa.Column("participant_id", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column("assigned_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.REAL(), nullable=False),
    )
    op.create_index("idx_play_assignments_participant", "play_assignments", ["participant_id"])


def downgrade() -> None:
    op.drop_index("idx_play_assignments_participant", table_name="play_assignments")
    op.drop_table("play_assignments")
    op.drop_table("play_members")
    op.drop_index("idx_play_instances_master", table_name="play_instances")
    op.drop_table("play_instances")
