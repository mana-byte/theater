"""Drop the checkpoints table and launch_provenance column.

The checkpoint/recovery subsystem has been removed. This migration drops
the ``checkpoints`` table and indexes for existing databases, and drops
the ``launch_provenance`` column from participants (introduced in 0012
for tree recovery, now unused). Fresh installs are unaffected — both
operations are guarded by inspector checks.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "checkpoints" in inspector.get_table_names():
        op.drop_index("idx_checkpoints_created_at", table_name="checkpoints")
        op.drop_index("idx_checkpoints_participant_name", table_name="checkpoints")
        op.drop_table("checkpoints")
    participant_cols = {c["name"] for c in inspector.get_columns("participants")}
    if "launch_provenance" in participant_cols:
        with op.batch_alter_table("participants") as batch_op:
            batch_op.drop_column("launch_provenance")


def downgrade() -> None:
    raise NotImplementedError(
        "checkpoints and launch_provenance have been removed; "
        "downgrade is not supported"
    )
