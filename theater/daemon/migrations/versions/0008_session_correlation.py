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
    # A NULL location before this instant means "the old daemon did not record
    # it". After this instant it means "the observer never admitted one". Keep
    # the epoch in meta so downgrade/re-upgrade cycles do not erase that
    # distinction and turn post-upgrade abstainers into legacy unknowns.
    op.get_bind().execute(
        sa.text(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('transcript_location_epoch', :value)"
        ),
        {"value": repr(time.time())},
    )


def downgrade() -> None:
    # Deliberately retain transcript_location_epoch in meta. If this revision
    # is later re-applied, rows created after the first upgrade must remain
    # post-epoch even though the downgraded schema could not store locations.
    op.drop_column("participants", "transcript_location")
    op.drop_column("participants", "transcript_domain")
    op.drop_column("participants", "session_correlation")
