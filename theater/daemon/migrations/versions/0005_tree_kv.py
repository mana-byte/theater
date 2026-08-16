"""Add the tree_kv table and its index.

A tree-scoped key-value scratchpad. Composite primary key on
(tree_root_id, repo_root, namespace, key) so each spawn tree can keep
per-namespace per-key values isolated from every other tree. An index on
(tree_root_id, repo_root) serves the common lookup pattern.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tree_kv",
        sa.Column("tree_root_id", sa.Text(), nullable=False),
        sa.Column("repo_root", sa.Text(), nullable=False),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tree_root_id", "repo_root", "namespace", "key"),
    )
    op.create_index(
        "idx_tree_kv_root",
        "tree_kv",
        ["tree_root_id", "repo_root"],
    )


def downgrade() -> None:
    op.drop_index("idx_tree_kv_root", table_name="tree_kv")
    op.drop_table("tree_kv")
