"""Record unavailable touch hashes.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("touch", sa.Column("sha_before_error", sa.Text(), nullable=True))
    op.add_column("touch", sa.Column("sha_after_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("touch", "sha_after_error")
    op.drop_column("touch", "sha_before_error")
