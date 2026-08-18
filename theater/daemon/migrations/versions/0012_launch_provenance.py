"""Add launch_provenance column to participants.

Persists durable launch provenance at spawn time — prompt, approval,
requested/resolved cwd, model, reasoning_effort, worktree details, base
branch, response_format, resume_session_id — as a compact JSON blob. Null for
EXTERNAL, ADOPTED, and pre-0012 SPAWNED participants.

This column is the immutable source-of-truth for orchestration-tree checkpoint
recovery: when a participant row is still present but the daemon has restarted,
recovery can cold-respawn the participant from provenance rather than requiring
the creator to remember every spawn argument.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("participants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("launch_provenance", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("participants", schema=None) as batch_op:
        batch_op.drop_column("launch_provenance")
