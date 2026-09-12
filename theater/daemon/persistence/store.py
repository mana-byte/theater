"""Store compatibility façade composing repositories over one Database."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection, Sequence
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

from sqlalchemy import insert, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.daemon import (
    BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
    BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
    BUS_KIND_TMUX_SERVER_RESTART,
    BUS_PARTICIPANT_PAGE_MAX_LIMIT,
    TMUX_SERVER_IDENTITY_META_KEY,
    TMUX_SERVER_RESTART_AFFECTED_IDS_LIMIT,
)
from theater.daemon.artifacts import OwnedArtifact
from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories.artifacts import ArtifactRepository
from theater.daemon.persistence.repositories.bus import BusRepository
from theater.daemon.persistence.repositories.channels import ChannelCredentialRepository
from theater.daemon.persistence.repositories.control_operations import (
    ControlOperationRepository,
)
from theater.daemon.persistence.repositories.jobs import JobRepository
from theater.daemon.persistence.repositories.mcp_plugins import McpPluginCredentialRepository
from theater.daemon.persistence.repositories.metadata import MetadataRepository
from theater.daemon.persistence.repositories.native_evidence import (
    NativeTerminalEvidenceRepository,
)
from theater.daemon.persistence.repositories.participants import ParticipantRepository
from theater.daemon.persistence.repositories.receipts import ReceiptRepository
from theater.daemon.persistence.repositories.runtime_bindings import RuntimeBindingRepository
from theater.daemon.persistence.repositories.scratchpad import ScratchpadRepository
from theater.daemon.persistence.repositories.statistics import StatisticsRepository
from theater.daemon.persistence.repositories.usage import UsageRepository
from theater.daemon.persistence.repositories.worktrees import WorktreeRepository
from theater.daemon.schema import bus, participants
from theater.harness.contracts.channels import ChannelKind
from theater.models import Job, Participant, Status, now

logger = logging.getLogger("theater.store")

BusListener = Callable[[dict], None]


class Store:
    """Compatibility façade over ``Database`` and explicit repositories."""

    def __init__(self, path: Path):
        self._db = Database(path)
        self.path = self._db.path
        self.engine = self._db.engine
        self.conn = self._db.conn

        self._participants = ParticipantRepository(self._db)
        self._artifacts = ArtifactRepository(self._db)
        self._jobs = JobRepository(self._db)
        self._bus = BusRepository(self._db)
        self._meta = MetadataRepository(self._db)
        self._receipts = ReceiptRepository(self._db, self._meta, self._participants)
        self._channels = ChannelCredentialRepository(self._db, self._meta, self._participants)
        self._mcp_plugins = McpPluginCredentialRepository(self._db, self._participants)
        self._scratchpad = ScratchpadRepository(self._db)
        self._worktrees = WorktreeRepository(self._db)
        self._usage = UsageRepository(self._db)
        self._statistics = StatisticsRepository(self._db)
        self._runtime_bindings = RuntimeBindingRepository(self._db)
        self._control_operations = ControlOperationRepository(self._db)
        self._native_evidence = NativeTerminalEvidenceRepository(self._db)
        self._bus_listeners: list[BusListener] = []

    def close(self) -> None:
        self._bus_listeners.clear()
        self._db.close()

    # ---- participants -------------------------------------------------

    def upsert_participant(self, p: Participant) -> None:
        self._participants.upsert(p)

    def get_participant(self, pid: str) -> Participant | None:
        return self._participants.get(pid)

    def find_by_pane(self, pane: str) -> Participant | None:
        return self._participants.find_by_pane(pane)

    def list_participants(
        self,
        *,
        include_dead: bool = False,
        ids: Sequence[str] | None = None,
        parent_id: str | None = None,
        after: tuple[float, str] | None = None,
        limit: int | None = None,
    ) -> list[Participant]:
        return self._participants.list_all(
            include_dead=include_dead,
            ids=ids,
            parent_id=parent_id,
            after=after,
            limit=limit,
        )

    def add_participant_artifacts(
        self, participant_id: str, artifacts: Sequence[OwnedArtifact]
    ) -> None:
        self._artifacts.add_many(participant_id, artifacts)

    def participant_artifacts(self, participant_id: str) -> tuple[OwnedArtifact, ...]:
        return self._artifacts.list_for(participant_id)

    def participant_artifact_owner_ids(self) -> tuple[str, ...]:
        return self._artifacts.owner_ids()

    def delete_participant_artifacts(self, participant_id: str, *, connection=None) -> int:
        return self._artifacts.delete_for(participant_id, connection=connection)

    def list_recent_dead(
        self, *, limit: int = 20, exclude_session_ids: set[str] | None = None
    ) -> list[Participant]:
        return self._participants.list_recent_dead(
            limit=limit, exclude_session_ids=exclude_session_ids
        )

    def children_of(self, pid: str) -> list[Participant]:
        return self._participants.children_of(pid)

    def set_status(self, pid: str, status: Status) -> None:
        self._participants.set_status(pid, status)

    def stamp_live_tmux_server_identity(
        self,
        identity: str,
        *,
        participant_ids: Sequence[str] | None = None,
    ) -> int:
        return self._participants.stamp_live_tmux_server_identity(
            identity,
            participant_ids=participant_ids,
        )

    def record_tmux_server_restart(
        self,
        *,
        server_identity: str,
        affected_ids: Sequence[str],
        newly_owned_ids: Sequence[str],
        incident: str,
        terminated_at: float,
    ) -> int:
        payload = {
            "incident": incident,
            "affected_count": len(affected_ids),
            "affected_ids": list(affected_ids[:TMUX_SERVER_RESTART_AFFECTED_IDS_LIMIT]),
        }
        listeners = tuple(self._bus_listeners)
        timestamp = now()
        with self.engine.begin() as conn:
            self._participants.mark_tmux_restarted(
                affected_ids,
                incident=incident,
                terminated_at=terminated_at,
                connection=conn,
            )
            self._participants.stamp_live_tmux_server_identity(
                server_identity,
                participant_ids=newly_owned_ids,
                connection=conn,
            )
            self._meta.set(
                TMUX_SERVER_IDENTITY_META_KEY,
                server_identity,
                connection=conn,
            )
            row_id = self._bus.append(
                BUS_KIND_TMUX_SERVER_RESTART,
                payload=payload,
                timestamp=timestamp,
                connection=conn,
            )
        if listeners:
            row = self._bus_row(
                row_id,
                timestamp,
                None,
                None,
                BUS_KIND_TMUX_SERVER_RESTART,
                json.dumps(payload),
            )
            self._notify_bus_listeners(
                [row],
                listeners,
            )
        return row_id

    def touch(self, pid: str) -> None:
        self._participants.touch(pid)

    def clear_resume_floor(self, pid: str) -> None:
        """Clear the resume floor column without touching any other field."""
        self._participants.clear_resume_floor(pid)

    def set_source_checkpoint(self, pid: str, checkpoint: str) -> None:
        self._participants.set_source_checkpoint(pid, checkpoint)

    def reparent_participant(self, pid: str, *, new_parent_id: str) -> None:
        """Set the parent_id of a participant."""
        self._participants.reparent(pid, new_parent_id=new_parent_id)

    def live_participants_in_cwd(self, cwd: str) -> list[Participant]:
        return self._participants.live_in_cwd(cwd)

    def live_count(self) -> int:
        """Count of participants whose status is not DEAD."""
        return self._participants.live_count()

    def addressable_count(self) -> int:
        """Count of participants matching ``Participant.addressable`` exactly."""
        return self._participants.addressable_count()

    def bind_operator_transcript(
        self,
        *,
        target: Participant,
        prior_owner: Participant | None,
        audit_payload: dict,
    ) -> int:
        """Move transcript ownership and append the audit row atomically."""
        target_values = ParticipantRepository._participant_values(target)
        listeners = tuple(self._bus_listeners)
        listener_rows: list[dict] = []
        with self.engine.begin() as conn:
            if prior_owner is not None:
                conn.execute(
                    update(participants)
                    .where(participants.c.id == prior_owner.id)
                    .values(
                        session_id=None,
                        session_correlation=None,
                        transcript_location=None,
                    )
                )
                unbind_payload = {
                    "actor_surface": "cli",
                    "target": prior_owner.id,
                    "transferred_to": target.id,
                    "path": audit_payload.get("path"),
                }
                unbind_ts = now()
                unbind_payload_text = json.dumps(unbind_payload)
                unbind_result = conn.execute(
                    insert(bus).values(
                        ts=unbind_ts,
                        from_id="cli",
                        to_id=prior_owner.id,
                        kind=BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
                        payload=unbind_payload_text,
                    )
                )
                if listeners:
                    unbind_pk = unbind_result.inserted_primary_key
                    assert unbind_pk is not None
                    listener_rows.append(
                        self._bus_row(
                            unbind_pk[0],
                            unbind_ts,
                            "cli",
                            prior_owner.id,
                            BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
                            unbind_payload_text,
                        )
                    )
            conn.execute(
                sqlite_insert(participants)
                .values(**target_values)
                .on_conflict_do_update(
                    index_elements=[participants.c.id],
                    set_={k: v for k, v in target_values.items() if k != "id"},
                )
            )
            bind_ts = now()
            bind_payload_text = json.dumps(audit_payload)
            result = conn.execute(
                insert(bus).values(
                    ts=bind_ts,
                    from_id="cli",
                    to_id=target.id,
                    kind=BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
                    payload=bind_payload_text,
                )
            )
            pk = result.inserted_primary_key
            assert pk is not None
            if listeners:
                listener_rows.append(
                    self._bus_row(
                        pk[0],
                        bind_ts,
                        "cli",
                        target.id,
                        BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
                        bind_payload_text,
                    )
                )
        if listeners:
            self._notify_bus_listeners(listener_rows, listeners)
        return pk[0]

    # ---- jobs ----------------------------------------------------------

    def create_job(self, job) -> None:
        self._jobs.create(job)

    def get_job(self, handle: str) -> Job | None:
        return self._jobs.get(handle)

    def finish_job(
        self,
        handle: str,
        *,
        state: str,
        result: str | None = None,
        error_code: str | None = None,
        finished_at: float | None = None,
        response_format: str | None = None,
        structured_result: str | None = None,
        structured_status: str | None = None,
    ) -> None:
        self._jobs.finish(
            handle,
            state=state,
            result=result,
            error_code=error_code,
            finished_at=finished_at,
            response_format=response_format,
            structured_result=structured_result,
            structured_status=structured_status,
        )

    def running_jobs_for_target(self, target_id: str) -> list[Job]:
        return self._jobs.running_for_target(target_id)

    def oldest_running_job_for_target(self, target_id: str) -> Job | None:
        """The longest-running job waiting on this participant, if any."""
        return self._jobs.oldest_running_for_target(target_id)

    def max_send_seq(self) -> int:
        """Highest numeric suffix across every send handle, 0 if none."""
        return self._jobs.max_send_seq()

    def spawn_prompts_for_targets(self, ids: Sequence[str]) -> dict[str, str | None]:
        return self._jobs.spawn_prompts_for_targets(list(ids))

    def active_job_count(self) -> int:
        """Count of jobs whose persisted state is ``running``."""
        return self._jobs.active_count()

    # ---- meta -----------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        return self._meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self._meta.set(key, value)

    def get_send_seq(self) -> int:
        return self._meta.get_send_seq()

    def set_send_seq(self, value: int) -> None:
        self._meta.set_send_seq(value)

    # ---- receipts -------------------------------------------------------

    def set_receipt_token(
        self,
        participant_id: str,
        token: str,
        *,
        token_path: str | None = None,
    ) -> None:
        self._receipts.set_token(participant_id, token, token_path=token_path)

    def get_receipt_token(self, participant_id: str) -> str | None:
        return self._receipts.get_token(participant_id)

    def renew_receipt_token(self, participant_id: str) -> None:
        self._receipts.renew_token(participant_id)

    def delete_receipt_token(self, participant_id: str) -> None:
        self._receipts.delete_token(participant_id)

    def cleanup_receipt_tokens(self) -> int:
        return self._receipts.cleanup_tokens()

    # ---- native channel credentials -----------------------------------

    def set_channel_credential(
        self,
        participant_id: str,
        *,
        harness: str,
        kind: ChannelKind,
        channel_id: str,
        token: str,
        token_path: str,
    ) -> None:
        """Persist one generic native channel credential."""
        self._channels.set(
            participant_id,
            harness=harness,
            kind=kind,
            channel_id=channel_id,
            token=token,
            token_path=token_path,
        )

    def get_channel_credential(
        self,
        participant_id: str,
        kind: ChannelKind,
        channel_id: str,
    ):
        """Read one generic native channel credential."""
        return self._channels.get(participant_id, kind, channel_id)

    def delete_channel_credentials(self, participant_id: str) -> None:
        """Delete all generic native channel credentials for one participant."""
        self._channels.delete_participant(participant_id)

    def cleanup_channel_credentials(self) -> int:
        return self._channels.cleanup()

    # ---- MCP-plugin sidecar credentials -------------------------------

    def set_mcp_plugin_credential(
        self,
        participant_id: str,
        *,
        plugin_name: str,
        api_version: int,
        credential_id: str,
        credential_verifier: str,
        grants,
        credential_path: str,
    ) -> None:
        """Persist one sidecar verifier and its exact launch-time grants."""
        self._mcp_plugins.set(
            participant_id,
            plugin_name=plugin_name,
            api_version=api_version,
            credential_id=credential_id,
            credential_verifier=credential_verifier,
            grants=grants,
            credential_path=credential_path,
        )

    def get_mcp_plugin_credential(self, credential_id: str):
        """Return an active sidecar credential record by its public selector."""
        return self._mcp_plugins.get_by_credential_id(credential_id)

    def mcp_plugin_credentials(self, participant_id: str):
        """Return durable attached-sidecar facts for one participant."""
        return self._mcp_plugins.list_for_participant(participant_id)

    def delete_mcp_plugin_credential(self, participant_id: str, plugin_name: str) -> None:
        """Revoke one sidecar before it can use its credential again."""
        self._mcp_plugins.delete_plugin(participant_id, plugin_name)

    def delete_mcp_plugin_credentials(self, participant_id: str) -> None:
        """Revoke every sidecar credential belonging to a participant."""
        self._mcp_plugins.delete_participant(participant_id)

    def cleanup_mcp_plugin_credentials(self) -> int:
        return self._mcp_plugins.cleanup()

    def record_transcript_receipt(
        self,
        participant_id: str,
        *,
        session_id: str,
        transcript_location: str,
    ) -> Participant | None:
        """Atomically persist exact receipt provenance for a participant."""
        return self._receipts.record_transcript_receipt(
            participant_id,
            session_id=session_id,
            transcript_location=transcript_location,
        )

    # ---- scratchpad -----------------------------------------------------

    def scratchpad_write(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        value: str,
        updated_by: str,
        key: str | None = None,
    ) -> str:
        return self._scratchpad.write(
            tree_root_id=tree_root_id,
            repo_root=repo_root,
            namespace=namespace,
            value=value,
            updated_by=updated_by,
            key=key,
        )

    def scratchpad_get(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        keys: list[str] | None = None,
    ) -> dict[str, str]:
        return self._scratchpad.get(
            tree_root_id=tree_root_id,
            repo_root=repo_root,
            namespace=namespace,
            keys=keys,
        )

    # ---- named worktrees ------------------------------------------------

    def get_named_worktree(self, *, repo_root: str, name: str) -> dict | None:
        return self._worktrees.get(repo_root=repo_root, name=name)

    def upsert_named_worktree(
        self,
        *,
        repo_root: str,
        name: str,
        branch: str,
        path: str,
        base_branch: str | None,
    ) -> None:
        self._worktrees.upsert(
            repo_root=repo_root,
            name=name,
            branch=branch,
            path=path,
            base_branch=base_branch,
        )

    def delete_named_worktree(self, *, repo_root: str, name: str) -> None:
        self._worktrees.delete(repo_root=repo_root, name=name)

    def named_worktree_by_path(self, path: str) -> dict | None:
        return self._worktrees.by_path(path)

    # ---- metrics --------------------------------------------------------

    def turn_outcomes(self, *, since: float | None = None) -> list[dict]:
        """How each harness's turns ended, counted per harness."""
        return self._statistics.turn_outcomes(since=since)

    def refusal_counts(self, *, since: float | None = None) -> dict[str, int]:
        """Sends refused before a job existed, counted by reason."""
        return self._bus.refusal_counts(since=since)

    # ---- usage ----------------------------------------------------------

    def record_usage(
        self,
        *,
        participant_id: str,
        tree_root_id: str | None,
        usage_key: str | None,
        ts: float,
        model: str | None,
        harness: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
        reasoning_output_tokens: int,
        cost_microcents: int,
    ) -> bool:
        """Insert one usage row, returning whether its native key was new."""
        return self._usage.record(
            participant_id=participant_id,
            tree_root_id=tree_root_id,
            usage_key=usage_key,
            ts=ts,
            model=model,
            harness=harness,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            reasoning_output_tokens=reasoning_output_tokens,
            cost_microcents=cost_microcents,
        )

    def usage_totals(self, *, since: float | None = None) -> dict:
        """Sum of all token and cost columns across the usage table."""
        return self._usage.totals(since=since)

    def usage_summary(self, *, since: float, average_since: float) -> dict[str, dict]:
        """All-time and two windowed usage totals in one table scan."""
        return self._usage.summary(since=since, average_since=average_since)

    def usage_by_harness(
        self, *, day_since: float, week_since: float, month_since: float
    ) -> list[dict]:
        """Aggregate the three local-calendar usage periods by durable harness."""
        return self._usage.by_harness(
            day_since=day_since, week_since=week_since, month_since=month_since
        )

    def usage_by_harness_detailed(
        self, *, day_since: float, week_since: float, month_since: float
    ) -> dict:
        """Aggregate the displayed periods by harness, model, and global total."""
        return self._usage.by_harness_detailed(
            day_since=day_since, week_since=week_since, month_since=month_since
        )

    # ---- native runtime wiring ------------------------------------------

    def runtime_transaction(self):
        """One explicit transaction for runtime-storage write boundaries."""
        return self.engine.begin()

    def upsert_runtime_binding(self, binding, *, connection=None) -> None:
        """Idempotently persist one participant runtime binding row."""
        self._runtime_bindings.upsert(binding, connection=connection)

    def get_runtime_binding(self, participant_id: str):
        return self._runtime_bindings.get(participant_id)

    def runtime_binding_by_native_session(self, native_session_id: str):
        """Exact identity lookup; the only non-heuristic session search."""
        return self._runtime_bindings.find_by_native_session(native_session_id)

    def runtime_bindings_for_recovery(self) -> list:
        """Bindings a daemon restart must reconcile before assuming loss."""
        return self._runtime_bindings.list_recoverable()

    def mark_runtime_backend_started(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        pid: int,
        started_at: float,
        connection=None,
    ) -> bool:
        """Record a verified backend pid, guarded by the expected generation."""
        return self._runtime_bindings.mark_backend_started(
            participant_id,
            backend_generation=backend_generation,
            pid=pid,
            started_at=started_at,
            connection=connection,
        )

    def bind_runtime_identity(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        native_session_id: str,
        protocol: str | None = None,
        protocol_version: str | None = None,
        native_version: str | None = None,
        compatibility_policy: str | None = None,
        updated_at: float,
        connection=None,
    ) -> bool:
        """Persist the exact native identity, guarded by the expected generation."""
        return self._runtime_bindings.bind_identity(
            participant_id,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            protocol=protocol,
            protocol_version=protocol_version,
            native_version=native_version,
            compatibility_policy=compatibility_policy,
            updated_at=updated_at,
            connection=connection,
        )

    def set_runtime_lifecycle(
        self,
        participant_id: str,
        phase,
        *,
        backend_generation: int,
        updated_at: float,
        connection=None,
    ) -> bool:
        """Advance one exact generation's lifecycle phase."""
        return self._runtime_bindings.set_lifecycle(
            participant_id,
            phase,
            backend_generation=backend_generation,
            updated_at=updated_at,
            connection=connection,
        )

    def delete_runtime_binding(self, participant_id: str, *, connection=None) -> None:
        self._runtime_bindings.delete(participant_id, connection=connection)

    def reserve_control_operation(self, operation, *, connection=None) -> None:
        """Persist one control operation before transmission."""
        self._control_operations.reserve(operation, connection=connection)

    def get_control_operation(self, operation_id: str):
        return self._control_operations.get(operation_id)

    def control_operations_for_job(self, job_handle: str) -> list:
        return self._control_operations.for_job(job_handle)

    def control_operation_for_native_turn(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
    ):
        """Exact native turn -> operation lookup for completion mapping."""
        return self._control_operations.for_native_turn(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
        )

    def queued_control_operations(self, participant_id: str) -> list:
        """Queued followups in FIFO order by allocated send sequence."""
        return self._control_operations.queued_for_participant(participant_id)

    def set_queued_control_payload(
        self, operation_id: str, payload: str, *, connection=None
    ) -> None:
        """Persist a queued operation's bounded causal context, not a turn binding."""
        self._control_operations.set_queued_payload(operation_id, payload, connection=connection)

    def set_queued_control_route(
        self,
        operation_id: str,
        *,
        transport,
        backend_generation: int | None,
        native_session_id: str | None,
        payload: str | None,
        updated_at: float,
        connection=None,
    ) -> bool:
        """Persist a dispatch-selected transport while a followup is still queued."""
        return self._control_operations.set_queued_route(
            operation_id,
            transport=transport,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            payload=payload,
            updated_at=updated_at,
            connection=connection,
        )

    def dispatched_control_operations(self, participant_id: str) -> list:
        """Operations whose transmission began and whose ack may never arrive."""
        return self._control_operations.dispatched_for_participant(participant_id)

    def execution_barrier_control_operations(self, participant_id: str) -> list:
        """Native prompt operations whose execution is still unresolved."""
        return self._control_operations.execution_barriers_for_participant(participant_id)

    def has_execution_barrier(self, participant_id: str) -> bool:
        """Whether unresolved native execution blocks automated prompts."""
        return self._control_operations.has_execution_barrier(participant_id)

    def unresolved_prompt_delivery_operations(self, participant_id: str) -> list:
        """Prompt rows still awaiting exact evidence or their deadline."""
        return self._control_operations.unresolved_prompt_deliveries_for_participant(participant_id)

    def control_operations_in_phases(self, participant_id: str, phases) -> list:
        """Every operation still in the given phases — the restart enumeration."""
        return self._control_operations.in_phases(participant_id, phases)

    def queued_control_operation_count(self, participant_id: str) -> int:
        return self._control_operations.pending_count_for_participant(participant_id)

    def mark_control_operation_dispatched(
        self,
        operation_id: str,
        *,
        native_session_id: str | None = None,
        native_turn_id: str | None = None,
        execution_barrier: bool | None = None,
        updated_at: float,
        connection=None,
    ) -> None:
        self._control_operations.mark_dispatched(
            operation_id,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
            execution_barrier=execution_barrier,
            updated_at=updated_at,
            connection=connection,
        )

    def settle_control_operation(
        self,
        operation_id: str,
        *,
        result,
        native_turn_id: str | None = None,
        error_code: str | None = None,
        error: str | None = None,
        execution_barrier: bool | None = None,
        updated_at: float,
        connection=None,
    ) -> None:
        self._control_operations.settle(
            operation_id,
            result=result,
            native_turn_id=native_turn_id,
            error_code=error_code,
            error=error,
            execution_barrier=execution_barrier,
            updated_at=updated_at,
            connection=connection,
        )

    def set_control_execution_barrier(
        self,
        operation_id: str,
        *,
        active: bool,
        updated_at: float,
        connection=None,
    ) -> None:
        """Persist whether an uncertain native prompt still blocks delivery."""
        self._control_operations.set_execution_barrier(
            operation_id,
            active=active,
            updated_at=updated_at,
            connection=connection,
        )

    def active_running_jobs_for_target(self, target_id: str) -> list[Job]:
        """Running jobs actually dispatched to the target, oldest first."""
        return self._control_operations.active_running_for_target(target_id)

    def allocate_control_queue_sequence(self, *, connection=None) -> int:
        """One queue position from the persisted send-sequence allocator."""
        return self._meta.allocate_send_seq(connection=connection)

    def get_control_queue_sequence(self, *, connection=None) -> int:
        """Current persisted send-sequence allocator value."""
        return self._meta.get_send_seq(connection=connection)

    def prune_control_operations(self, *, older_than: float, limit: int | None = None) -> int:
        """Bounded prune of settled operations."""
        kwargs: dict = {"older_than": older_than}
        if limit is not None:
            kwargs["limit"] = limit
        return self._control_operations.prune(**kwargs)

    def record_native_terminal_evidence(self, evidence, *, connection=None) -> bool:
        """Persist terminal evidence; first write wins."""
        return self._native_evidence.record(evidence, connection=connection)

    def get_native_terminal_evidence(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
    ):
        return self._native_evidence.get(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
        )

    def native_terminal_evidence_for_participant(self, participant_id: str) -> list:
        return self._native_evidence.for_participant(participant_id)

    def prune_native_terminal_evidence(self, *, older_than: float, limit: int | None = None) -> int:
        """Bounded prune of terminal evidence."""
        kwargs: dict = {"older_than": older_than}
        if limit is not None:
            kwargs["limit"] = limit
        return self._native_evidence.prune(**kwargs)

    # ---- bus ----------------------------------------------------------

    def register_bus_listener(self, listener: BusListener) -> None:
        """Register one synchronous best-effort post-commit bus listener."""
        if listener not in self._bus_listeners:
            self._bus_listeners.append(listener)

    def unregister_bus_listener(self, listener: BusListener) -> None:
        """Remove a bus listener; repeated removal is harmless."""
        with suppress(ValueError):
            self._bus_listeners.remove(listener)

    @staticmethod
    def _bus_row(
        row_id: int,
        timestamp: float,
        from_id: str | None,
        to_id: str | None,
        kind: str,
        payload_text: str | None,
    ) -> dict:
        return {
            "id": row_id,
            "ts": timestamp,
            "from_id": from_id,
            "to_id": to_id,
            "kind": kind,
            "payload": json.loads(payload_text) if payload_text else None,
        }

    def _notify_bus_listeners(self, rows: list[dict], listeners: tuple[BusListener, ...]) -> None:
        """Notify listeners after commit without letting one failure escape."""
        for row in rows:
            for listener in listeners:
                try:
                    listener(deepcopy(row))
                except Exception:
                    logger.exception("bus listener failed for row %s", row.get("id"))

    def bus_append(
        self,
        kind: str,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        payload: dict | None = None,
    ) -> int:
        listeners = tuple(self._bus_listeners)
        timestamp = now() if listeners else None
        row_id = self._bus.append(
            kind,
            from_id=from_id,
            to_id=to_id,
            payload=payload,
            timestamp=timestamp,
        )
        if listeners:
            assert timestamp is not None
            payload_text = json.dumps(payload) if payload else None
            row = self._bus_row(row_id, timestamp, from_id, to_id, kind, payload_text)
            self._notify_bus_listeners([row], listeners)
        return row_id

    def bus_page_for_participant(
        self,
        participant_id: str,
        *,
        before_id: int | str | None = None,
        limit: int = BUS_PARTICIPANT_PAGE_MAX_LIMIT,
        kinds: Collection[str],
    ) -> list[dict]:
        return self._bus.page_for_participant(
            participant_id,
            before_id=before_id,
            limit=limit,
            kinds=kinds,
        )

    def bus_record_for_participant(
        self,
        participant_id: str,
        row_id: int,
        *,
        kinds: Collection[str],
    ) -> dict | None:
        return self._bus.record_for_participant(participant_id, row_id, kinds=kinds)

    def bus_tail(self, limit: int = 100, *, after_id: int = 0) -> list[dict]:
        return self._bus.tail(limit, after_id=after_id)

    def observation_error_active(self, participant_id: str, code: str) -> bool:
        """Whether an observation error remains uncleared in the audit stream."""
        return self._bus.observation_error_active(participant_id, code)

    def observation_error_timestamp(self, participant_id: str, code: str) -> float | None:
        """The wall-clock ``ts`` of the most recent uncleared observation error."""
        return self._bus.observation_error_timestamp(participant_id, code)
