"""Persist transcript correlation facts across daemon restarts.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import time

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("participants", sa.Column("session_correlation", sa.Text(), nullable=True))
    op.add_column("participants", sa.Column("transcript_domain", sa.Text(), nullable=True))
    op.add_column("participants", sa.Column("transcript_location", sa.Text(), nullable=True))
    # Keep the epoch in meta so downgrade/re-upgrade cycles preserve the NULL-location distinction.
    op.get_bind().execute(
        sa.text(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('transcript_location_epoch', :value)"
        ),
        {"value": repr(time.time())},
    )


def downgrade() -> None:
    # Deliberately retain transcript_location_epoch in meta so re-applied upgrades stay post-epoch.
    op.drop_column("participants", "transcript_location")
    op.drop_column("participants", "transcript_domain")
    op.drop_column("participants", "session_correlation")
