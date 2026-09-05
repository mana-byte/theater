"""Purge touch rows whose paths cannot be safely resolved.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM touch
        WHERE replace(path, char(92), '/') GLOB '/*'
           OR replace(path, char(92), '/') GLOB '[A-Za-z]:/*'
           OR replace(path, char(92), '/') = '..'
           OR replace(path, char(92), '/') GLOB '../*'
           OR replace(path, char(92), '/') GLOB '*/../*'
           OR replace(path, char(92), '/') GLOB '*/..'
        """
    )


def downgrade() -> None:
    # Deleted path content cannot be reconstructed.
    pass
