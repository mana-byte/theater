"""Add the named_worktrees table and its index.

Named shared worktrees: multiple live children can share one linked
worktree (same directory, same branch, same index/HEAD). The key is
(repo_root, name) so the same name in two repositories does not collide.
Only Theater-created named worktrees appear here — a join reuses a row
the daemon recognises, never an arbitrary pre-existing branch or directory.
An index on ``path`` serves the teardown membership check (is any live
participant still in this directory?).

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "named_worktrees",
        sa.Column("repo_root", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("base_branch", sa.Text(), nullable=True),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.PrimaryKeyConstraint("repo_root", "name"),
    )
    op.create_index(
        "idx_named_worktrees_path",
        "named_worktrees",
        ["path"],
    )


def downgrade() -> None:
    op.drop_index("idx_named_worktrees_path", table_name="named_worktrees")
    op.drop_table("named_worktrees")
