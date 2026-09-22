"""Persist exact workspace cleanup intent and outcome."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("deletion_prior_state", sa.Text(), nullable=True))
    op.add_column("workspaces", sa.Column("cleanup_force", sa.Integer(), nullable=True))
    op.add_column("workspaces", sa.Column("cleanup_delete_branch", sa.Integer(), nullable=True))
    op.add_column("workspaces", sa.Column("cleanup_force_branch", sa.Integer(), nullable=True))
    op.add_column("workspaces", sa.Column("cleanup_result", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workspaces", "cleanup_result")
    op.drop_column("workspaces", "cleanup_force_branch")
    op.drop_column("workspaces", "cleanup_delete_branch")
    op.drop_column("workspaces", "cleanup_force")
    op.drop_column("workspaces", "deletion_prior_state")
