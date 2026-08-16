"""Add structured-result columns to the jobs table.

Three nullable TEXT columns:
- response_format: the raw serialized JSON schema hint.
- structured_result: the complete bare JSON response without clipping.
- structured_status: null when JSON was not requested, later "parsed" or
  "unavailable".

All three default to null, so existing rows and existing callers are
unaffected — legacy result semantics are unchanged.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("response_format", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("structured_result", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("structured_status", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("structured_status")
        batch_op.drop_column("structured_result")
        batch_op.drop_column("response_format")
