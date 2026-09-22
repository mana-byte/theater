"""Record the operation that owns a Theater workspace creation intent."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("creation_operation_id", sa.Text(), nullable=True))
    op.create_index("idx_workspaces_creation", "workspaces", ["creation_operation_id"])


def downgrade() -> None:
    op.drop_index("idx_workspaces_creation", table_name="workspaces")
    op.drop_column("workspaces", "creation_operation_id")
