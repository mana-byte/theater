"""Backfill ``creator_name`` on checkpoints for DBs migrated before it existed.

``creator_name`` was added to the schema by amending the already-released
revision 0011 in place. Alembic never re-runs a revision it has already
recorded, so any DB that ran 0011 in its original form (before the amendment)
is stamped at head yet permanently missing the column — every
``list_checkpoints`` query then fails with ``no such column:
checkpoints.creator_name``.

This revision adds the column only when it is absent, so it is a no-op on
fresh DBs (which got the column from the amended 0011) and a repair on
already-migrated DBs. Idempotency is required precisely because both shapes
exist in the wild at head.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _has_creator_name() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Total against a DB where checkpoints does not exist yet (unreachable via
    # the 0006->0014 chain, but keeps this a pure predicate rather than a raise).
    if "checkpoints" not in inspector.get_table_names():
        return False
    return any(c["name"] == "creator_name" for c in inspector.get_columns("checkpoints"))


def upgrade() -> None:
    if _has_creator_name():
        return
    with op.batch_alter_table("checkpoints", schema=None) as batch_op:
        batch_op.add_column(sa.Column("creator_name", sa.Text(), nullable=True))


def downgrade() -> None:
    # No-op on purpose. creator_name is owned by revision 0011, which drops it in
    # its own downgrade; dropping it here too would double-drop when unwinding
    # past 0011. Downgrade to 0013 therefore correctly leaves the column intact.
    pass
