"""Reserve a deferred spawn: a participant, its one task job and its gate, with no launch."""

from __future__ import annotations

from theater.daemon.handoff.models import HandoffGate
from theater.daemon.plays.models import PlayPlan
from theater.daemon.rails import check_budget, check_depth
from theater.daemon.spawning.provider_launch_parts._host import ParticipantLaunchHost
from theater.daemon.terminals import ProviderUnavailable
from theater.frontend.capabilities import TERMINAL_PROVIDER_CAPABILITY
from theater.harness import get as get_harness
from theater.models import BadRequest, Participant, now


class SpawnDeferral(ParticipantLaunchHost):
    def require_known_provider(self, requested: object) -> None:
        """Refuse now a provider no wait could ever produce; only its health may change later.

        An unregistered provider or one without terminal creation is configuration. Checking at
        admission means such a task fails where it is asked for, not hours later or never.
        """
        token = (
            requested
            if isinstance(requested, str) and requested
            else self.default_provider_selector
        )
        record = self.store.providers.get(token) or self.store.providers.get_by_selector(token)
        if record is None:
            raise ProviderUnavailable(token, "not_registered")
        if TERMINAL_PROVIDER_CAPABILITY not in record.capabilities:
            raise ProviderUnavailable(record.provider_id, "missing_terminal_create_capability")

    def reserve_deferred_spawn(
        self,
        *,
        harness: str,
        cwd: str,
        parent_id: str | None,
        name: str | None,
        description: str | None,
        task: str,
        response_format: str | None,
        gate: HandoffGate,
        play: PlayPlan | None = None,
    ) -> Participant:
        """Durably hold the participant name and the one job handle the caller will await.

        No worktree, no argv and no terminal exist until the gate opens: the reservation is only
        rows, so a restart loses nothing and a cancellation releases nothing. The play rules are
        checked now and again at launch, which alone seats the child.
        """
        adapter = get_harness(harness)
        if self._resolve_binary(adapter.binary) is None:
            raise BadRequest(f"{adapter.binary!r} is not on PATH")
        timestamp = now()
        rails = self.daemon.config.rails
        with self.store.write_unit() as unit:
            if (
                parent_id is not None
                and self._participant_in_connection(parent_id, unit.connection) is None
            ):
                raise BadRequest(f"no initiating participant {parent_id!r} exists")
            check_depth(self.store, parent_id, cap=rails.depth_cap)
            check_budget(self.store, parent_id, limit=rails.budget)
            self.daemon.plays.check_spawn(parent_id, play or PlayPlan(), connection=unit.connection)
            participant = self.registry.create_spawned(
                harness=harness,
                cwd=cwd,
                parent_id=parent_id,
                has_prompt=bool(task),
                name=name,
                description=description,
                connection=unit.connection,
            )
            job = self._reserve_spawn_job(
                unit,
                participant_id=participant.id,
                parent_id=parent_id,
                client_id="private-rpc",
                prompt=task,
                response_format=response_format,
                timestamp=timestamp,
            )
            self.store.create_handoff_gate(
                HandoffGate(
                    job_handle=participant.id,
                    caller_id=parent_id or "cli",
                    source_handle=gate.source_handle,
                    source_participant_id=gate.source_participant_id,
                    source_name=gate.source_name,
                    task_kind=gate.task_kind,
                    request=gate.request,
                    state=gate.state,
                    error_code=None,
                    error=None,
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
                connection=unit.connection,
            )
            first = self.store.journal.current_sequence(connection=unit.connection) + 1
            self.store.journal.append_group(
                unit,
                [
                    self._participant_event(
                        participant, timestamp, revision=first, connection=unit.connection
                    ),
                    self._job_event(job, timestamp, revision=first + 1),
                ],
            )
            unit.after_commit(lambda: self.daemon.jobs.track(participant.id))
            unit.after_commit(
                lambda: self.registry.remember_reserved_name(participant.id, participant.name or "")
            )
            unit.after_commit(lambda: self._announce_deferred(participant, job, cwd))
        return participant

    def _announce_deferred(self, participant, job, cwd: str) -> None:
        self.store.bus_append(
            "participant.created",
            to_id=participant.id,
            from_id=participant.parent_id,
            payload={
                "tier": str(participant.tier),
                "harness": participant.harness,
                "cwd": cwd,
                "has_prompt": True,
            },
        )
        self.store.bus_append(
            "job.created",
            from_id=job.caller_id,
            to_id=participant.id,
            payload={"handle": participant.id, "kind": "spawn"},
        )


__all__ = ["SpawnDeferral"]
