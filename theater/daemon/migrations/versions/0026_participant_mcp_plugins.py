"""Persist participant-scoped MCP-plugin sidecar grants.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "participant_mcp_plugins",
        sa.Column("participant_id", sa.Text(), nullable=False),
        sa.Column("plugin_name", sa.Text(), nullable=False),
        sa.Column("api_version", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.Text(), nullable=False),
        sa.Column("credential_verifier", sa.Text(), nullable=False),
        sa.Column("grants", sa.Text(), nullable=False),
        sa.Column("credential_path", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("participant_id", "plugin_name"),
        sa.UniqueConstraint("credential_id"),
    )
    op.create_index(
        "idx_participant_mcp_plugins_participant",
        "participant_mcp_plugins",
        ["participant_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_participant_mcp_plugins_participant", table_name="participant_mcp_plugins")
    op.drop_table("participant_mcp_plugins")
