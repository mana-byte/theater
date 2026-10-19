"""Composition façade for provider identity, reports, bindings, and callbacks."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from theater.constants.daemon import terminal_runtime_identity_meta_key
from theater.daemon.events.publication import catalog_invalidated_event, terminal_binding_event
from theater.daemon.operations import OperationOutcome, OperationService
from theater.daemon.terminals.bindings import TerminalBindingService, TerminalIdentityMismatch
from theater.daemon.terminals.connections import (
    CallbackOutcomeUnknown,
    ProviderCallbackRejected,
    ProviderConnectionService,
    ProviderUnavailable,
    StaleGeneration,
)
from theater.daemon.terminals.recovery import ProviderReceiptError, ProviderReceiptReconciler
from theater.daemon.terminals.registry import ProviderRegistry, provider_event
from theater.daemon.terminals.runtime_identity import (
    RuntimeIdentity,
    RuntimeIdentityInvalid,
    binding_identity_duty,
    report_identity,
    usable_runtime_identity,
)
from theater.models import (
    Participant,
    ProviderRecord,
    TerminalBindingRecord,
    TheaterError,
    new_id,
    now,
)


class ProviderReportInvalid(TheaterError):
    code = "bad_request"


class StaleReportRevision(TheaterError):
    code = "stale_generation"

    def __init__(
        self,
        provider_id: str,
        generation: int,
        report_revision: int,
        last_report_revision: int | None,
    ) -> None:
        self.details = {
            "provider_id": provider_id,
            "provider_generation": generation,
            "report_revision": report_revision,
            "last_report_revision": last_report_revision,
        }
        super().__init__(
            "provider report revisions must be strictly increasing for the current generation"
        )


@dataclass(frozen=True, slots=True)
class _ReportContext:
    provider_id: str
    generation: int
    revision: int
    snapshot: bool
    presence_invalidated: bool
    invalidated_terminals: tuple[str, ...] | None
    terminals: Sequence[Mapping[str, object]]
    receipts: Sequence[Mapping[str, object]]
    ignored: tuple[str, ...]
    has_terminal_facts: bool
    inventory_verified: bool
    runtime_identity: RuntimeIdentity | None
    health_snapshot: str
    health_changes: bool
    timestamp: float


@dataclass(frozen=True, slots=True)
class _ReportSettlement:
    restored: Sequence[str]
    reconciled: Sequence[str]
    deferred: Sequence[str]


class TerminalProviderService:
    def __init__(
        self,
        store,
        operations: OperationService,
        *,
        monotonic: Callable[[], float] | None = None,
        clock: Callable[[], float] = now,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.connections = ProviderConnectionService(
            store,
            monotonic=monotonic or time.monotonic,
            wall_clock=clock,
            id_factory=id_factory or new_id,
        )
        self.registry = ProviderRegistry(
            store,
            operations,
            health=self.connections.health,
            clock=clock,
            **({"id_factory": id_factory} if id_factory is not None else {}),
        )
        self.bindings = TerminalBindingService(store)
        self.recovery = ProviderReceiptReconciler(store, operations)
        self._store = store
        self._operations = operations
        self._clock = clock
        self._startup_recovering = False
        self._presence_invalidated: Callable[..., None] | None = None
        self._runtime_restart_finalizer: Callable[[Sequence[Participant], bool], None] | None = None

    def configure_presence_invalidation(self, callback: Callable[..., None]) -> None:
        self._presence_invalidated = callback

    def configure_runtime_restart_finalizer(
        self, callback: Callable[[Sequence[Participant], bool], None]
    ) -> None:
        self._runtime_restart_finalizer = callback

    def begin_startup_recovery(self) -> None:
        self._startup_recovering = True

    def finish_startup_recovery(self) -> None:
        self._startup_recovering = False

    def authenticate_handshake(
        self, provider_id: str, credential: str, *, callback: bool
    ) -> tuple[int, str | None]:
        if self._startup_recovering:
            raise ProviderUnavailable(provider_id, "daemon_recovery_in_progress")
        if callback:
            return self.connections.acquire_callback(provider_id, credential)
        return self.connections.rpc_generation(provider_id, credential), None

    def negotiated_limits(self, provider_id: str, generation: int) -> Mapping[str, object]:
        return self.connections.negotiated_limits(provider_id, generation)

    async def serve_callback(self, context, reader, writer) -> None:
        await self.connections.serve(context, reader, writer)

    def configure_recovery(self, *, controls, jobs) -> None:
        """Install live notifications after durable recovery is composed."""
        self.recovery.configure_runtime(controls=controls, jobs=jobs)

    def binding_projection(self, binding: TerminalBindingRecord) -> dict[str, object]:
        result = self.bindings.project(binding)
        provider_health = self.connections.health(binding.provider_id)
        if not self.connections.is_current(binding.provider_id, binding.provider_generation):
            result["health"] = "offline" if provider_health == "offline" else "reconciling"
        return result

    def heartbeat(self, provider_id: str, generation: int, report_revision: int) -> dict:
        health = self.connections.health(provider_id)
        self._accept_report_revision(provider_id, generation, report_revision, health=health)
        return {
            "provider_id": provider_id,
            "provider_generation": generation,
            "report_revision": report_revision,
            "health": self.connections.health(provider_id),
        }

    def report(
        self,
        provider_id: str,
        generation: int,
        report_revision: int,
        facts: Mapping[str, object] | None,
        *,
        snapshot: bool = False,
    ) -> dict:
        context = self._prepare_report(
            provider_id, generation, report_revision, facts, snapshot=snapshot
        )
        settlement = self._apply_report(context)
        return self._report_response(context, settlement)

    def _prepare_report(
        self,
        provider_id: str,
        generation: int,
        report_revision: int,
        facts: Mapping[str, object] | None,
        *,
        snapshot: bool,
    ) -> _ReportContext:
        if not self.connections.is_current(provider_id, generation):
            raise StaleGeneration(provider_id, generation)
        presence_invalidated = False if facts is None else facts.get("presence_invalidated", False)
        if type(presence_invalidated) is not bool:
            raise ProviderReportInvalid(
                "provider report facts.presence_invalidated must be a boolean"
            )
        invalidated_terminals = self._invalidated_terminals(facts)
        terminals = self._terminal_facts(facts)
        receipts = self._receipt_facts(facts)
        ignored = self._validate_receipts(provider_id, receipts)
        has_terminal_facts = facts is not None and "terminals" in facts
        inventory_verified = facts is not None and facts.get("complete") is True
        if inventory_verified and not has_terminal_facts:
            raise ProviderReportInvalid(
                "a complete provider report requires an explicit terminals array"
            )
        provider = self._store.providers.get(provider_id)
        assert provider is not None
        runtime_identity = self._runtime_identity(
            provider,
            facts,
            terminals,
            inventory_complete=inventory_verified,
        )
        health_snapshot = self.connections.health(provider_id)
        # Participant projections derive route availability from provider health.
        health_changes = (
            "online" if inventory_verified else health_snapshot
        ) != health_snapshot or (not snapshot and provider.last_report_revision is None)
        return _ReportContext(
            provider_id=provider_id,
            generation=generation,
            revision=report_revision,
            snapshot=snapshot,
            presence_invalidated=presence_invalidated,
            invalidated_terminals=invalidated_terminals,
            terminals=terminals,
            receipts=receipts,
            ignored=ignored,
            has_terminal_facts=has_terminal_facts,
            inventory_verified=inventory_verified,
            runtime_identity=runtime_identity,
            health_snapshot=health_snapshot,
            health_changes=health_changes,
            timestamp=self._clock(),
        )

    @staticmethod
    def _invalidated_terminals(facts: Mapping[str, object] | None) -> tuple[str, ...] | None:
        # Omitted means every terminal; an empty list means none.
        if facts is None or "invalidated_terminals" not in facts:
            return None
        scope = facts["invalidated_terminals"]
        if not isinstance(scope, list) or not all(type(item) is str for item in scope):
            raise ProviderReportInvalid(
                "provider report facts.invalidated_terminals must be a list of terminal ids"
            )
        return tuple(scope)

    def _apply_report(self, context: _ReportContext) -> _ReportSettlement:
        with self._store.write_unit() as unit:
            if context.snapshot:
                record = self._store.providers.get(context.provider_id, connection=unit.connection)
                if record is None or record.generation != context.generation:
                    raise StaleGeneration(context.provider_id, context.generation)
                if (
                    record.last_report_revision is not None
                    and context.revision <= record.last_report_revision
                ):
                    raise StaleReportRevision(
                        context.provider_id,
                        context.generation,
                        context.revision,
                        record.last_report_revision,
                    )
            elif not self._store.providers.accept_report_revision(
                context.provider_id,
                generation=context.generation,
                report_revision=context.revision,
                updated_at=context.timestamp,
                connection=unit.connection,
            ):
                self._raise_stale_report(
                    context.provider_id,
                    context.generation,
                    context.revision,
                    connection=unit.connection,
                )
            if context.inventory_verified and context.runtime_identity is not None:
                self._reconcile_runtime_identity(
                    unit,
                    provider_id=context.provider_id,
                    identity=context.runtime_identity,
                    timestamp=context.timestamp,
                )
            restored, changed_bindings = (
                self.bindings.reconcile(
                    context.provider_id,
                    context.generation,
                    context.revision,
                    context.terminals,
                    complete=context.inventory_verified,
                    timestamp=context.timestamp,
                    connection=unit.connection,
                    publish_refresh=context.health_changes,
                )
                if context.has_terminal_facts
                else ((), ())
            )
            events, first_revision = self._report_events(unit, context, changed_bindings)
            try:
                reconciled, deferred, recovery_events = self.recovery.reconcile(
                    unit,
                    provider_id=context.provider_id,
                    current_generation=context.generation,
                    report_revision=context.revision,
                    inventory_complete=context.inventory_verified,
                    terminals=context.terminals,
                    receipts=context.receipts,
                    timestamp=context.timestamp,
                    first_revision=first_revision + len(events),
                )
            except ProviderReceiptError as exc:
                raise ProviderReportInvalid(str(exc)) from exc
            events.extend(recovery_events)
            if events:
                self._store.journal.append_group(unit, events)
            if context.inventory_verified:
                unit.after_commit(
                    lambda: self.connections.mark_online(context.provider_id, context.generation)
                )
            unit.after_commit(
                lambda: self.connections.renew(context.provider_id, context.generation)
            )
            if context.presence_invalidated and self._presence_invalidated is not None:
                callback = self._presence_invalidated
                unit.after_commit(
                    lambda: callback(
                        context.provider_id,
                        context.generation,
                        invalidated_terminals=context.invalidated_terminals,
                    )
                )
        return _ReportSettlement(restored, reconciled, deferred)

    def _report_events(self, unit, context: _ReportContext, changed_bindings) -> tuple[list, int]:
        record = self._store.providers.get(context.provider_id, connection=unit.connection)
        assert record is not None
        health = "online" if context.inventory_verified else context.health_snapshot
        first_revision = self._store.journal.current_sequence(connection=unit.connection) + 1
        events = (
            [provider_event(record, health, context.timestamp, revision=first_revision)]
            if context.health_changes
            else []
        )
        if context.inventory_verified and context.health_snapshot != "online":
            events.append(
                catalog_invalidated_event(
                    context.provider_id,
                    revision=first_revision + len(events),
                    recorded_at=context.timestamp,
                    reason="provider_online",
                )
            )
        projected_online_provider = (
            (context.provider_id, context.generation) if context.inventory_verified else None
        )
        for participant_id in changed_bindings:
            binding = self._store.terminal_bindings.get(participant_id, connection=unit.connection)
            assert binding is not None
            events.append(
                terminal_binding_event(
                    self._store,
                    binding,
                    unit.connection,
                    revision=first_revision + len(events),
                    recorded_at=context.timestamp,
                    projected_online_provider=projected_online_provider,
                )
            )
        return events, first_revision

    def _report_response(self, context: _ReportContext, settlement: _ReportSettlement) -> dict:
        health = self.connections.health(context.provider_id)
        deferred_ids = set(settlement.deferred)
        acknowledged = tuple(
            dict.fromkeys(
                str(receipt["operation_id"])
                for receipt in context.receipts
                if str(receipt["operation_id"]) not in deferred_ids
            )
        )
        return {
            "provider_id": context.provider_id,
            "provider_generation": context.generation,
            "report_revision": context.revision,
            "health": health,
            "restored_participant_ids": list(settlement.restored),
            "reconciled_operation_ids": list(settlement.reconciled),
            "ignored_operation_ids": list(context.ignored),
            "acknowledged_operation_ids": list(acknowledged),
            "deferred_operation_ids": list(settlement.deferred),
        }

    async def inventory(
        self, provider_id: str, generation: int, *, cursor: str | None = None, limit: int = 200
    ) -> Mapping[str, object]:
        params: dict[str, object] = {"provider_generation": generation, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        result = await self.connections.request(
            provider_id, generation, "terminal.inventory", params
        )
        revision = result["report_revision"]
        terminals = result["terminals"]
        assert isinstance(revision, int) and isinstance(terminals, list)
        report_facts: dict[str, object] = {
            "terminals": terminals,
            "complete": result["complete"],
        }
        for key in ("runtime_identity", "tmux_server_identity"):
            if key in result:
                report_facts[key] = result[key]
        self.report(
            provider_id,
            generation,
            revision,
            report_facts,
            snapshot=True,
        )
        return result

    async def inspect(
        self,
        provider_id: str,
        generation: int,
        terminal_id: str,
        incarnation: str,
        *,
        screen_max_bytes: int = 0,
    ) -> Mapping[str, object]:
        expected = self._store.terminal_bindings.find_terminal(
            provider_id, terminal_id, incarnation
        )
        params: dict[str, object] = {
            "provider_generation": generation,
            "terminal_id": terminal_id,
            "terminal_incarnation": incarnation,
        }
        if screen_max_bytes:
            params["screen_max_bytes"] = screen_max_bytes
        if expected is not None:
            params["expected_terminal"] = {
                "provider_id": provider_id,
                "provider_generation": generation,
                "terminal_id": terminal_id,
                "terminal_incarnation": incarnation,
                "occupant": dict(expected.occupant_evidence),
                "process": None if expected.process_facts is None else dict(expected.process_facts),
            }
        result = await self.connections.request(
            provider_id,
            generation,
            "terminal.inspect",
            params,
        )
        if expected is not None:
            current = self._store.terminal_bindings.get(expected.participant_id)
            if current != expected:
                raise TerminalIdentityMismatch(provider_id, terminal_id, "binding_changed")
        terminal = result["terminal"]
        revision = result["report_revision"]
        assert isinstance(terminal, Mapping) and isinstance(revision, int)
        self.report(
            provider_id,
            generation,
            revision,
            {"terminals": [terminal], "complete": False},
            snapshot=True,
        )
        return result

    async def dispatch_operation(
        self,
        provider_id: str,
        generation: int,
        method: str,
        params: Mapping[str, object],
    ) -> OperationOutcome:
        self._validate_operation_target(provider_id, generation, method, params)
        try:
            result = await self.connections.request(provider_id, generation, method, params)
        except CallbackOutcomeUnknown as exc:
            return OperationOutcome.uncertain(
                phase="provider_ack_lost",
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )
        except ProviderCallbackRejected as exc:
            return OperationOutcome.failed(
                phase="provider_rejected",
                error={"code": exc.code, "message": str(exc)},
            )
        outcome = result.get("outcome") if method == "terminal.create" else result.get("delivery")
        if outcome == "accepted":
            return OperationOutcome.succeeded(phase="provider_acknowledged", result=dict(result))
        if outcome == "unknown":
            return OperationOutcome.uncertain(
                phase="provider_outcome_unknown",
                error=self._result_error(result, "provider returned an unknown outcome"),
            )
        return OperationOutcome.failed(
            phase="provider_rejected",
            error=self._result_error(result, "provider rejected the terminal request"),
        )

    def _validate_operation_target(
        self,
        provider_id: str,
        generation: int,
        method: str,
        params: Mapping[str, object],
    ) -> None:
        operation_id = params.get("operation_id")
        if not isinstance(operation_id, str):
            raise TypeError("mutating provider callbacks require an operation id")
        operation = self._operations.get(operation_id)
        if method == "terminal.create":
            return
        participant_id = params.get("participant_id")
        if not isinstance(participant_id, str):
            raise TypeError("terminal mutations require a participant id")
        binding = self._store.terminal_bindings.get(participant_id)
        if binding is None:
            raise TerminalIdentityMismatch(provider_id, str(params.get("terminal_id")), "missing")
        expected = (
            provider_id,
            generation,
            params.get("terminal_id"),
            params.get("terminal_incarnation"),
        )
        bound = (
            binding.provider_id,
            binding.provider_generation,
            binding.terminal_id,
            binding.terminal_incarnation,
        )
        dispatched = (
            operation.dispatch_provider_id,
            operation.dispatch_provider_generation,
            operation.dispatch_terminal_id,
            operation.dispatch_terminal_incarnation,
        )
        occupant_id = binding.occupant_evidence.get("occupant_id")
        if (
            expected != bound
            or dispatched != bound
            or params.get("expected_occupant") != occupant_id
        ):
            raise TerminalIdentityMismatch(provider_id, binding.terminal_id, "dispatch_target")

    def _validate_receipts(
        self, provider_id: str, receipts: Sequence[Mapping[str, object]]
    ) -> tuple[str, ...]:
        try:
            return self.recovery.validate(provider_id, receipts)
        except ProviderReceiptError as exc:
            raise ProviderReportInvalid(str(exc)) from exc

    def _reconcile_runtime_identity(
        self,
        unit,
        *,
        provider_id: str,
        identity: RuntimeIdentity,
        timestamp: float,
    ) -> None:
        provider = self._store.providers.get(provider_id, connection=unit.connection)
        assert provider is not None
        affected = self.bindings.participants_on_other_runtime(
            provider_id,
            identity.value,
            provider.kind,
            connection=unit.connection,
        )
        for legacy in (False, True):
            if legacy and not (identity.legacy or identity.dual):
                continue
            if not legacy and identity.legacy:
                continue
            participants = tuple(
                participant for participant, evidence in affected if evidence.legacy is legacy
            )
            meta_key = terminal_runtime_identity_meta_key(provider_id, legacy=legacy)
            previous = self._store.get_meta(meta_key, connection=unit.connection)
            changed_runtime = usable_runtime_identity(previous) not in (None, identity.value)
            if participants or changed_runtime:
                self._store.record_terminal_runtime_restart(
                    provider_id=provider_id,
                    runtime_identity=identity.value,
                    legacy=legacy,
                    affected_ids=[participant.id for participant in participants],
                    incident=new_id(),
                    terminated_at=timestamp,
                    unit=unit,
                )
                if participants and self._runtime_restart_finalizer is not None:
                    callback = self._runtime_restart_finalizer
                    unit.after_commit(
                        lambda group=participants, provenance=legacy, notify=callback: notify(
                            group, provenance
                        )
                    )
            elif previous != identity.value:
                self._store.set_meta(meta_key, identity.value, connection=unit.connection)

    async def aclose(self) -> None:
        await self.connections.aclose()

    def _accept_report_revision(
        self,
        provider_id: str,
        generation: int,
        report_revision: int,
        *,
        health: str,
    ) -> None:
        if not self.connections.is_current(provider_id, generation):
            raise StaleGeneration(provider_id, generation)
        timestamp = self._clock()
        with self._store.write_unit() as unit:
            record = self._store.providers.get(provider_id, connection=unit.connection)
            assert record is not None
            # A heartbeat changes nothing durable except the report revision; only
            # first contact for the generation is material, as in the report path.
            first_contact = record.last_report_revision is None
            if not self._store.providers.accept_report_revision(
                provider_id,
                generation=generation,
                report_revision=report_revision,
                updated_at=timestamp,
                connection=unit.connection,
            ):
                self._raise_stale_report(
                    provider_id,
                    generation,
                    report_revision,
                    connection=unit.connection,
                )
            if first_contact:
                record = self._store.providers.get(provider_id, connection=unit.connection)
                assert record is not None
                self._store.journal.append_group(
                    unit,
                    [
                        provider_event(
                            record,
                            health,
                            timestamp,
                            revision=self._store.journal.current_sequence(
                                connection=unit.connection
                            )
                            + 1,
                        )
                    ],
                )
            unit.after_commit(lambda: self.connections.renew(provider_id, generation))

    def _raise_stale_report(
        self,
        provider_id: str,
        generation: int,
        report_revision: int,
        *,
        connection,
    ) -> None:
        record = self._store.providers.get(provider_id, connection=connection)
        if record is None or record.generation != generation:
            raise StaleGeneration(provider_id, generation)
        raise StaleReportRevision(
            provider_id,
            generation,
            report_revision,
            record.last_report_revision,
        )

    @staticmethod
    def _terminal_facts(
        facts: Mapping[str, object] | None,
    ) -> Sequence[Mapping[str, object]]:
        if facts is None:
            return ()
        if "terminals" not in facts:
            return ()
        raw = facts["terminals"]
        if (
            not isinstance(raw, list)
            or len(raw) > 500
            or not all(isinstance(item, Mapping) for item in raw)
        ):
            raise ProviderReportInvalid("provider report facts.terminals must be a bounded array")
        return raw

    @staticmethod
    def _receipt_facts(
        facts: Mapping[str, object] | None,
    ) -> Sequence[Mapping[str, object]]:
        if facts is None:
            return ()
        if "receipts" not in facts:
            return ()
        raw = facts["receipts"]
        if (
            not isinstance(raw, list)
            or len(raw) > 500
            or not all(isinstance(item, Mapping) for item in raw)
        ):
            raise ProviderReportInvalid("provider report facts.receipts must be a bounded array")
        return raw

    def _runtime_identity(
        self,
        provider: ProviderRecord,
        facts: Mapping[str, object] | None,
        terminals: Sequence[Mapping[str, object]],
        *,
        inventory_complete: bool,
    ) -> RuntimeIdentity | None:
        needs_duty = inventory_complete or (facts is not None and "runtime_identity" in facts)
        if not needs_duty:
            needs_duty = any(
                isinstance(occupant := terminal.get("occupant"), Mapping)
                and "runtime_identity" in occupant
                for terminal in terminals
            )
        retained_generic = retained_legacy = False
        if needs_duty:
            bindings = self.bindings.list(provider.provider_id)
            binding_generic, binding_legacy = binding_identity_duty(
                (binding.occupant_evidence for binding in bindings), provider.kind
            )
            generic_key = terminal_runtime_identity_meta_key(provider.provider_id, legacy=False)
            legacy_key = terminal_runtime_identity_meta_key(provider.provider_id, legacy=True)
            retained_generic = self._store.get_meta(generic_key) is not None or binding_generic
            retained_legacy = self._store.get_meta(legacy_key) is not None or binding_legacy
        try:
            return report_identity(
                provider.kind,
                provider.capabilities,
                facts,
                terminals,
                complete=inventory_complete,
                retained_generic=retained_generic,
                retained_legacy=retained_legacy,
            )
        except RuntimeIdentityInvalid as exc:
            raise ProviderReportInvalid(str(exc)) from exc

    @staticmethod
    def _result_error(result: Mapping[str, object], fallback: str) -> dict[str, object]:
        error = result.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("code"), str):
            return dict(error)
        return {"code": "provider_unavailable", "message": fallback}


__all__ = ["ProviderReportInvalid", "StaleReportRevision", "TerminalProviderService"]
