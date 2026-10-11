"""Strict response contracts: custom registry, repair attempts with their fence, and the audit.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "response_contracts",
        sa.Column("creator_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("schema", sa.Text(), nullable=False),
        sa.Column("created_at", sa.REAL(), nullable=False),
    )
    op.create_table(
        "job_repairs",
        sa.Column("attempt_id", sa.Text(), primary_key=True),
        sa.Column("job_handle", sa.Text(), nullable=False),
        sa.Column("participant_id", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("fence", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("backend_generation", sa.Integer()),
        sa.Column("native_session_id", sa.Text()),
        sa.Column("source_turn_id", sa.Text()),
        sa.Column("native_turn_id", sa.Text()),
        sa.Column("turn_proven", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("reason", sa.Text()),
        sa.Column("outcome", sa.Text()),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
        sa.UniqueConstraint("job_handle", "attempt", name="uq_job_repairs_job_attempt"),
    )
    op.create_index("idx_job_repairs_participant", "job_repairs", ["participant_id", "fence"])
    op.create_table(
        "job_contract_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_handle", sa.Text(), nullable=False),
        sa.Column("participant_id", sa.Text()),
        sa.Column("at", sa.REAL(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text()),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_job_contract_audit_job", "job_contract_audit", ["job_handle"])


def downgrade() -> None:
    op.drop_index("idx_job_contract_audit_job", table_name="job_contract_audit")
    op.drop_table("job_contract_audit")
    op.drop_index("idx_job_repairs_participant", table_name="job_repairs")
    op.drop_table("job_repairs")
    op.drop_table("response_contracts")
