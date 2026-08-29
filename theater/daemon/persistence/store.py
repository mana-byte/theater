"""Store compatibility façade composing repositories over one Database.

Preserves the exact public surface of the original ``Store``: ``path``,
``engine``, ``conn``, and every method signature and return type. Callers
that monkeypatch individual methods continue to work because every method
is explicit on the class — no ``__getattr__``, no mixins, no dynamic
delegation.

Cross-table atomic operations (``bind_operator_transcript``) remain in the
façade because they span multiple repositories within one transaction.
"""

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
from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories.bus import BusRepository
from theater.daemon.persistence.repositories.channels import ChannelCredentialRepository
from theater.daemon.persistence.repositories.jobs import JobRepository
from theater.daemon.persistence.repositories.metadata import MetadataRepository
from theater.daemon.persistence.repositories.participants import ParticipantRepository
from theater.daemon.persistence.repositories.receipts import ReceiptRepository
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
    """Compatibility façade over ``Database`` and explicit repositories.

    Deliberately synchronous. Calls are local, sub-millisecond, and bounded
    by the number of participants (tens, not thousands).
    """

    def __init__(self, path: Path):
        self._db = Database(path)
        self.path = self._db.path
        self.engine = self._db.engine
        self.conn = self._db.conn

        self._participants = ParticipantRepository(self._db)
        self._jobs = JobRepository(self._db)
        self._bus = BusRepository(self._db)
        self._meta = MetadataRepository(self._db)
        self._receipts = ReceiptRepository(self._db, self._meta, self._participants)
        self._channels = ChannelCredentialRepository(self._db, self._meta, self._participants)
        self._scratchpad = ScratchpadRepository(self._db)
        self._worktrees = WorktreeRepository(self._db)
        self._usage = UsageRepository(self._db)
        self._statistics = StatisticsRepository(self._db)
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
