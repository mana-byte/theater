"""Add RC10 durable orchestration records behind the drained-upgrade guard."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

from theater.daemon.persistence.database import ensure_rc9_upgrade_allowed

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_rc9_upgrade_allowed(op.get_bind())

    op.add_column(
        "participants",
        sa.Column("origin", sa.Text(), server_default=sa.text("'external'"), nullable=False),
    )
    op.add_column(
        "participants",
        sa.Column(
            "control_owner_kind",
            sa.Text(),
            server_default=sa.text("'local_operator'"),
            nullable=False,
        ),
    )
    op.add_column("participants", sa.Column("control_owner_id", sa.Text(), nullable=True))
    op.add_column(
        "participants",
        sa.Column("control_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("participants", sa.Column("workspace_id", sa.Text(), nullable=True))
    op.execute("UPDATE participants SET origin = tier")
    op.execute(
        "UPDATE participants SET control_owner_kind = 'participant', "
        "control_owner_id = parent_id WHERE parent_id IS NOT NULL"
    )
    op.create_index(
        "idx_participants_control_owner",
        "participants",
        ["control_owner_kind", "control_owner_id"],
    )
    op.create_index("idx_participants_workspace", "participants", ["workspace_id"])

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.alter_column("caller_id", existing_type=sa.Text(), nullable=True)
        batch_op.add_column(sa.Column("actor_client_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("actor_participant_id", sa.Text(), nullable=True))
    op.create_index("idx_jobs_actor", "jobs", ["actor_client_id", "actor_participant_id"])

    for name in (
        "provider_id",
        "terminal_id",
        "terminal_incarnation",
    ):
        op.add_column("control_operations", sa.Column(name, sa.Text(), nullable=True))
    op.add_column(
        "control_operations", sa.Column("provider_generation", sa.Integer(), nullable=True)
    )

    _create_provider_tables()
    _create_operation_tables()
    _create_workspace_tables()
    _create_journal_and_scratchpad()
    _backfill_named_workspaces()


def _create_provider_tables() -> None:
    op.create_table(
        "providers",
        sa.Column("provider_id", sa.Text(), primary_key=True),
        sa.Column("selector", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("credential_verifier", sa.Text(), nullable=False),
        sa.Column("configuration_version", sa.Integer(), nullable=False),
        sa.Column("capabilities", sa.Text(), nullable=False),
        sa.Column("limits", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_report_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
    )
    op.create_index("uq_providers_selector", "providers", ["selector"], unique=True)

    op.create_table(
        "terminal_bindings",
        sa.Column("participant_id", sa.Text(), primary_key=True),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("provider_generation", sa.Integer(), nullable=False),
        sa.Column("terminal_id", sa.Text(), nullable=False),
        sa.Column("terminal_incarnation", sa.Text(), nullable=False),
        sa.Column("process_facts", sa.Text(), nullable=True),
        sa.Column("occupant_evidence", sa.Text(), nullable=False),
        sa.Column("health", sa.Text(), nullable=False),
        sa.Column("report_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
    )
    op.create_index(
        "uq_terminal_bindings_identity",
        "terminal_bindings",
        ["provider_id", "terminal_id", "terminal_incarnation"],
        unique=True,
    )
    op.create_index("idx_terminal_bindings_provider", "terminal_bindings", ["provider_id"])


def _create_operation_tables() -> None:
    op.create_table(
        "public_operations",
        sa.Column("operation_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("actor_client_id", sa.Text(), nullable=False),
        sa.Column("actor_participant_id", sa.Text(), nullable=True),
        sa.Column("target_ids", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("control_operation_id", sa.Text(), nullable=True),
        sa.Column("job_handle", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("dispatch_provider_id", sa.Text(), nullable=True),
        sa.Column("dispatch_provider_generation", sa.Integer(), nullable=True),
        sa.Column("dispatch_terminal_id", sa.Text(), nullable=True),
        sa.Column("dispatch_terminal_incarnation", sa.Text(), nullable=True),
        sa.Column("dispatch_backend_generation", sa.Integer(), nullable=True),
        sa.Column("dispatch_native_session_id", sa.Text(), nullable=True),
        sa.Column("dispatch_native_turn_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
        sa.Column("settled_at", sa.REAL(), nullable=True),
    )
    op.create_index("idx_public_operations_state", "public_operations", ["state", "updated_at"])
    op.create_index("idx_public_operations_job", "public_operations", ["job_handle"])
    op.create_index(
        "uq_public_operations_control",
        "public_operations",
        ["control_operation_id"],
        unique=True,
        sqlite_where=sa.text("control_operation_id IS NOT NULL"),
    )

    op.create_table(
        "idempotency_records",
        sa.Column("client_id", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("payload_digest", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=True),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.Column("settled_at", sa.REAL(), nullable=True),
        sa.Column("retain_until", sa.REAL(), nullable=True),
    )
    op.create_index("idx_idempotency_retention", "idempotency_records", ["retain_until"])
    op.create_index("idx_idempotency_operation", "idempotency_records", ["operation_id"])


def _create_workspace_tables() -> None:
    op.create_table(
        "workspaces",
        sa.Column("workspace_id", sa.Text(), primary_key=True),
        sa.Column("ownership_kind", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("canonical_repository_root", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("resolved_base_commit", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("deletion_operation_id", sa.Text(), nullable=True),
        sa.Column("deletion_token", sa.Text(), nullable=True),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
    )
    op.create_index(
        "uq_workspaces_active_path",
        "workspaces",
        ["path"],
        unique=True,
        sqlite_where=sa.text("state != 'removed'"),
    )
    op.create_index("idx_workspaces_owner", "workspaces", ["ownership_kind", "owner_id"])
    op.create_index("idx_workspaces_state", "workspaces", ["state"])

    op.create_table(
        "workspace_usages",
        sa.Column("usage_id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("holder_kind", sa.Text(), nullable=False),
        sa.Column("holder_id", sa.Text(), nullable=False),
        sa.Column("acquired_at", sa.REAL(), nullable=False),
        sa.Column("released_at", sa.REAL(), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_workspace_usages_workspace",
        "workspace_usages",
        ["workspace_id", "acquired_at"],
    )
    op.create_index(
        "uq_workspace_usages_live_holder",
        "workspace_usages",
        ["workspace_id", "holder_kind", "holder_id"],
        unique=True,
        sqlite_where=sa.text("released_at IS NULL"),
    )

    op.create_table(
        "launch_reservations",
        sa.Column("operation_id", sa.Text(), primary_key=True),
        sa.Column("participant_id", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("workspace_usage_id", sa.Text(), nullable=True),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("launch_facts", sa.Text(), nullable=False),
        sa.Column("artifact_refs", sa.Text(), nullable=False),
        sa.Column("dispatch_marker", sa.Text(), nullable=True),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
    )
    op.create_index(
        "uq_launch_reservations_participant",
        "launch_reservations",
        ["participant_id"],
        unique=True,
    )
    op.create_index("idx_launch_reservations_provider", "launch_reservations", ["provider_id"])


def _create_journal_and_scratchpad() -> None:
    op.create_table(
        "orchestration_events",
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("transaction_id", sa.Text(), nullable=False),
        sa.Column("event_index", sa.Integer(), nullable=False),
        sa.Column("ending_sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("entity_revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.REAL(), nullable=False),
    )
    op.create_index(
        "uq_orchestration_events_transaction_index",
        "orchestration_events",
        ["transaction_id", "event_index"],
        unique=True,
    )
    op.create_index("idx_orchestration_events_recorded", "orchestration_events", ["recorded_at"])

    op.create_table(
        "global_scratchpad",
        sa.Column("namespace", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
        sa.Column("expires_at", sa.REAL(), nullable=False),
        sa.Column("actor_client_id", sa.Text(), nullable=True),
        sa.Column("actor_participant_id", sa.Text(), nullable=True),
    )
    op.create_index("idx_global_scratchpad_expiry", "global_scratchpad", ["expires_at"])
    op.execute("DELETE FROM tree_kv")

    op.execute(
        sa.text("INSERT OR IGNORE INTO meta (key, value) VALUES (:key, :value)").bindparams(
            key="orchestration_stream_id", value=uuid.uuid4().hex
        )
    )
    op.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('orchestration_sequence', '0')")


def _backfill_named_workspaces() -> None:
    op.execute(
        """
        INSERT OR IGNORE INTO workspaces (
            workspace_id, ownership_kind, owner_id, path,
            canonical_repository_root, branch, resolved_base_commit, name,
            state, created_at, updated_at
        )
        SELECT
            'rc9-named-' || lower(hex(randomblob(12))), 'theater', 'theater', path,
            repo_root, branch, NULL, name, 'reconcile', created_at, created_at
        FROM named_worktrees
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM meta WHERE key IN ('orchestration_stream_id', 'orchestration_sequence')"
    )
    op.drop_index("idx_global_scratchpad_expiry", table_name="global_scratchpad")
    op.drop_table("global_scratchpad")
    op.drop_index("idx_orchestration_events_recorded", table_name="orchestration_events")
    op.drop_index("uq_orchestration_events_transaction_index", table_name="orchestration_events")
    op.drop_table("orchestration_events")
    op.drop_index("idx_launch_reservations_provider", table_name="launch_reservations")
    op.drop_index("uq_launch_reservations_participant", table_name="launch_reservations")
    op.drop_table("launch_reservations")
    op.drop_index("uq_workspace_usages_live_holder", table_name="workspace_usages")
    op.drop_index("idx_workspace_usages_workspace", table_name="workspace_usages")
    op.drop_table("workspace_usages")
    op.drop_index("idx_workspaces_state", table_name="workspaces")
    op.drop_index("idx_workspaces_owner", table_name="workspaces")
    op.drop_index("uq_workspaces_active_path", table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_index("idx_idempotency_operation", table_name="idempotency_records")
    op.drop_index("idx_idempotency_retention", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_index("uq_public_operations_control", table_name="public_operations")
    op.drop_index("idx_public_operations_job", table_name="public_operations")
    op.drop_index("idx_public_operations_state", table_name="public_operations")
    op.drop_table("public_operations")
    op.drop_index("idx_terminal_bindings_provider", table_name="terminal_bindings")
    op.drop_index("uq_terminal_bindings_identity", table_name="terminal_bindings")
    op.drop_table("terminal_bindings")
    op.drop_index("uq_providers_selector", table_name="providers")
    op.drop_table("providers")

    for name in (
        "terminal_incarnation",
        "terminal_id",
        "provider_generation",
        "provider_id",
    ):
        op.drop_column("control_operations", name)

    op.drop_index("idx_jobs_actor", table_name="jobs")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("actor_participant_id")
        batch_op.drop_column("actor_client_id")
        batch_op.alter_column("caller_id", existing_type=sa.Text(), nullable=False)

    op.drop_index("idx_participants_workspace", table_name="participants")
    op.drop_index("idx_participants_control_owner", table_name="participants")
    for name in (
        "workspace_id",
        "control_revision",
        "control_owner_id",
        "control_owner_kind",
        "origin",
    ):
        op.drop_column("participants", name)
