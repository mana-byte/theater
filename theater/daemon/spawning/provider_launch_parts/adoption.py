"""External participant adoption and terminal identity binding."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from theater.daemon.events.publication import (
    terminal_binding_event,
)
from theater.daemon.operations import (
    DispatchIntent,
    OperationOutcome,
    PreparedOperation,
)
from theater.daemon.operations.projection import operation_event_payload
from theater.daemon.persistence.transactions import WriteUnit
from theater.daemon.spawning.provider_launch_parts._host import ParticipantLaunchHost
from theater.daemon.terminals import ProviderUnavailable, TerminalIdentityMismatch
from theater.daemon.terminals.bindings import ensure_terminal_unbound
from theater.models import (
    BadRequest,
    Job,
    JournalEventRecord,
    Participant,
    ParticipantOrigin,
    PublicOperationRecord,
    TerminalBindingRecord,
    Tier,
    WorkspaceUsageRecord,
    new_id,
    now,
)
from theater.provenance import is_trusted_provenance


class ParticipantAdoption(ParticipantLaunchHost):
    def adopt(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Accept explicit adoption; the detached task always performs a fresh inspect."""
        captured: dict[str, object] = {}

        def prepare(operation_id: str, unit: WriteUnit) -> PreparedOperation:
            provider_id, generation = self._select_provider(
                params["provider_id"], unit, allow_selector=False
            )
            actor_participant_id = self._optional_text(params.get("initiating_participant_id"))
            if (
                actor_participant_id is not None
                and self._participant_in_connection(actor_participant_id, unit.connection) is None
            ):
                raise BadRequest(f"no initiating participant {actor_participant_id!r} exists")
            requested = self._optional_text(params.get("participant_id"))
            created_participant = requested is None
            participant: Participant | None
            if requested is None:
                participant = self.registry.create_spawned(
                    pid=new_id(),
                    harness="unknown",
                    cwd="",
                    parent_id=None,
                    has_prompt=False,
                    tier=Tier.ADOPTED,
                    origin=ParticipantOrigin.ADOPTED,
                    connection=unit.connection,
                )
                if participant.name is not None:
                    reserved_participant_id = participant.id
                    reserved_name = participant.name
                    unit.after_commit(
                        lambda: self.registry.remember_reserved_name(
                            reserved_participant_id, reserved_name
                        )
                    )
            else:
                participant = self._participant_in_connection(requested, unit.connection)
                if participant is None:
                    raise BadRequest(f"no participant {requested!r} exists")
                if participant.origin not in {ParticipantOrigin.EXTERNAL, None}:
                    raise BadRequest("adoption can attach only an existing external participant")
                if (
                    self.store.terminal_bindings.get(participant.id, connection=unit.connection)
                    is not None
                ):
                    raise TerminalIdentityMismatch(
                        provider_id, str(params["terminal_id"]), "participant_already_bound"
                    )
            timestamp = now()
            first_revision = self.store.journal.current_sequence(connection=unit.connection) + 1
            captured.update(
                participant=participant,
                provider_id=provider_id,
                provider_generation=generation,
                created_participant=created_participant,
            )
            return PreparedOperation(
                record=PublicOperationRecord(
                    operation_id=operation_id,
                    kind="adopt",
                    actor_client_id=client_id,
                    actor_participant_id=actor_participant_id,
                    target_ids=(participant.id,),
                    state="accepted",
                    phase="adoption_reserved",
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
                response={
                    "operation_id": operation_id,
                    "state": "accepted",
                    "participant_id": participant.id,
                    "job_handle": None,
                },
                events=(
                    self._participant_event(
                        participant,
                        timestamp,
                        revision=first_revision,
                        connection=unit.connection,
                    ),
                ),
            )

        acceptance = self.operations.accept_operation(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.participants.adopt",
            params=params,
            prepare=prepare,
        )
        if acceptance.replayed:
            return acceptance.response
        participant = captured["participant"]
        assert isinstance(participant, Participant)
        provider_generation = captured["provider_generation"]
        assert type(provider_generation) is int

        async def side_effect() -> OperationOutcome:
            try:
                inspected = await self.terminals.inspect(
                    str(captured["provider_id"]),
                    provider_generation,
                    str(params["terminal_id"]),
                    str(params["terminal_incarnation"]),
                )
                terminal = self._inspected_terminal(
                    inspected,
                    provider_id=str(captured["provider_id"]),
                    terminal_id=str(params["terminal_id"]),
                )
                self._validate_adoption(
                    participant,
                    terminal,
                    inspected,
                    provider_id=str(captured["provider_id"]),
                    provider_generation=provider_generation,
                    terminal_id=str(params["terminal_id"]),
                    terminal_incarnation=str(params["terminal_incarnation"]),
                )
                attached = self._bind_adoption(
                    acceptance.record.operation_id, participant, terminal
                )
                return OperationOutcome.succeeded(
                    phase="terminal_adopted", result={"participant_id": attached.id}
                )
            except (BadRequest, ProviderUnavailable, TerminalIdentityMismatch) as exc:
                if captured["created_participant"]:
                    self._rollback_adoption_reservation(participant.id)
                return OperationOutcome.failed(
                    phase="adoption_refused",
                    error={"code": exc.code, "message": str(exc)},
                )
            except Exception as exc:
                if captured["created_participant"]:
                    self._rollback_adoption_reservation(participant.id)
                return OperationOutcome.failed(
                    phase="adoption_refused",
                    error={"code": "internal", "message": str(exc)},
                )

        self.operations.start(
            acceptance.record.operation_id,
            dispatch=DispatchIntent(phase="terminal_inspection_started"),
            side_effect=side_effect,
        )
        return acceptance.response

    def _bind_adoption(
        self,
        operation_id: str,
        participant: Participant,
        terminal: Mapping[str, object],
    ) -> Participant:
        with self.store.write_unit() as unit:
            current = self._participant_in_connection(participant.id, unit.connection)
            if current is None:
                raise RuntimeError("adoption participant disappeared")
            binding = self._binding(current.id, terminal)
            self._ensure_terminal_unbound(binding, unit.connection)
            self.store.terminal_bindings.bind(binding, connection=unit.connection)
            if current.origin is ParticipantOrigin.ADOPTED:
                current.tier = Tier.ADOPTED
            occupant = terminal["occupant"]
            assert isinstance(occupant, Mapping)
            harness = occupant.get("harness")
            if isinstance(harness, str) and harness:
                current.harness = harness
            cwd = occupant.get("cwd")
            if isinstance(cwd, str) and cwd:
                current.cwd = cwd
            self.registry.persist_in_connection(current, unit.connection)
            operation = self._persist_dispatch_identity(operation_id, terminal, unit)
            self._append_binding_events(unit, current, binding, None, operation=operation)
        return current

    def _persist_dispatch_identity(
        self, operation_id: str, terminal: Mapping[str, object], unit: WriteUnit
    ) -> PublicOperationRecord:
        operation = self.store.operations.get(operation_id, connection=unit.connection)
        if operation is None:
            raise RuntimeError("public operation disappeared before terminal binding")
        occupant = terminal["occupant"]
        process = terminal.get("process")
        assert isinstance(occupant, Mapping)
        assert process is None or isinstance(process, Mapping)
        generation = terminal["provider_generation"]
        if type(generation) is not int:
            raise TypeError("terminal provider generation must be an integer")
        updated = replace(
            operation,
            dispatch_provider_id=str(terminal["provider_id"]),
            dispatch_provider_generation=generation,
            dispatch_terminal_id=str(terminal["terminal_id"]),
            dispatch_terminal_incarnation=str(terminal["terminal_incarnation"]),
            dispatch_terminal_occupant_evidence=dict(occupant),
            dispatch_terminal_process_facts=None if process is None else dict(process),
            updated_at=now(),
        )
        if not self.store.operations.replace(
            updated,
            expected_state=operation.state,
            expected_updated_at=operation.updated_at,
            connection=unit.connection,
        ):
            raise RuntimeError("public operation changed during terminal binding")
        return updated

    def _ensure_terminal_unbound(self, candidate: TerminalBindingRecord, connection) -> None:
        ensure_terminal_unbound(self.store, candidate, connection)

    def _append_binding_events(
        self,
        unit: WriteUnit,
        participant: Participant,
        binding: TerminalBindingRecord,
        usage: WorkspaceUsageRecord | None,
        *,
        operation: PublicOperationRecord,
        completed_job: Job | None = None,
    ) -> None:
        first = self.store.journal.current_sequence(connection=unit.connection) + 1
        timestamp = now()
        events = [
            self._participant_event(
                participant,
                timestamp,
                revision=first,
                connection=unit.connection,
            ),
            terminal_binding_event(
                self.store,
                binding,
                unit.connection,
                revision=first + 1,
                recorded_at=timestamp,
            ),
        ]
        if usage is not None:
            events.append(
                self._workspace_usage_event(
                    usage,
                    timestamp,
                    revision=first + 2,
                    connection=unit.connection,
                )
            )
        if completed_job is not None:
            events.append(self._job_event(completed_job, timestamp, revision=0))
        events.append(
            JournalEventRecord(
                kind="operation.updated",
                entity_id=operation.operation_id,
                entity_revision=first + len(events),
                payload=operation_event_payload(operation),
                recorded_at=timestamp,
            )
        )
        self.store.journal.append_group(unit, events)

    def _validate_adoption(
        self,
        participant: Participant,
        terminal: Mapping[str, object],
        inspected: Mapping[str, object],
        *,
        provider_id: str,
        provider_generation: int,
        terminal_id: str,
        terminal_incarnation: str,
    ) -> None:
        identity = (
            terminal.get("provider_id"),
            terminal.get("provider_generation"),
            terminal.get("terminal_id"),
            terminal.get("terminal_incarnation"),
        )
        if identity != (provider_id, provider_generation, terminal_id, terminal_incarnation):
            raise TerminalIdentityMismatch(provider_id, terminal_id, "inspection_identity")
        occupant = terminal.get("occupant")
        if not isinstance(occupant, Mapping):
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "occupant"
            )
        harness = occupant.get("harness")
        if not isinstance(harness, str) or not harness:
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "harness"
            )
        if participant.harness not in {"unknown", harness}:
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "harness"
            )
        process = terminal.get("process")
        if participant.pid is not None and (
            not isinstance(process, Mapping) or process.get("pid") != participant.pid
        ):
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "process"
            )
        lifecycle = inspected.get("lifecycle")
        if isinstance(lifecycle, Mapping) and lifecycle.get("alive") is False:
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "occupant_not_live"
            )
        if participant.session_id and is_trusted_provenance(participant.session_correlation):
            if not isinstance(lifecycle, Mapping):
                raise TerminalIdentityMismatch(
                    str(terminal["provider_id"]),
                    str(terminal["terminal_id"]),
                    "trusted_identity_missing",
                )
            reported_session = lifecycle.get("native_session_id") or lifecycle.get("session_id")
            if reported_session != participant.session_id:
                raise TerminalIdentityMismatch(
                    str(terminal["provider_id"]),
                    str(terminal["terminal_id"]),
                    "trusted_identity",
                )

    @staticmethod
    def _inspected_terminal(
        inspected: Mapping[str, object], *, provider_id: str, terminal_id: str
    ) -> Mapping[str, object]:
        terminal = inspected.get("terminal")
        if not isinstance(terminal, Mapping):
            raise TerminalIdentityMismatch(provider_id, terminal_id, "inspection_missing_identity")
        return terminal

    @staticmethod
    def _binding(participant_id: str, terminal: Mapping[str, object]) -> TerminalBindingRecord:
        occupant = terminal["occupant"]
        process = terminal.get("process")
        assert isinstance(occupant, Mapping)
        assert process is None or isinstance(process, Mapping)
        generation = terminal["provider_generation"]
        if type(generation) is not int:
            raise TypeError("terminal provider generation must be an integer")
        timestamp = now()
        return TerminalBindingRecord(
            participant_id=participant_id,
            provider_id=str(terminal["provider_id"]),
            provider_generation=generation,
            terminal_id=str(terminal["terminal_id"]),
            terminal_incarnation=str(terminal["terminal_incarnation"]),
            occupant_evidence=dict(occupant),
            process_facts=None if process is None else dict(process),
            health="healthy",
            report_revision=0,
            created_at=timestamp,
            updated_at=timestamp,
        )
