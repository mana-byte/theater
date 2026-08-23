"""Index participant directions for bounded historical bus pages.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_bus_from_id_id", "bus", ["from_id", "id"], unique=False)
    op.create_index("idx_bus_to_id_id", "bus", ["to_id", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_bus_to_id_id", table_name="bus")
    op.drop_index("idx_bus_from_id_id", table_name="bus")
