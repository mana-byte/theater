"""Composition façade for provider identity, reports, bindings, and callbacks."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence

from theater.daemon.operations import OperationOutcome, OperationService
from theater.daemon.terminals.bindings import TerminalBindingService, TerminalIdentityMismatch
from theater.daemon.terminals.connections import (
    CallbackOutcomeUnknown,
    ProviderCallbackRejected,
    ProviderConnectionService,
    StaleGeneration,
)
from theater.daemon.terminals.registry import ProviderRegistry, provider_event
from theater.models import JournalEventRecord, TerminalBindingRecord, TheaterError, new_id, now


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
        self._store = store
        self._operations = operations
        self._clock = clock

    def authenticate_handshake(
        self, provider_id: str, credential: str, *, callback: bool
    ) -> tuple[int, str | None]:
        if callback:
            return self.connections.acquire_callback(provider_id, credential)
        return self.connections.rpc_generation(provider_id, credential), None

    def negotiated_limits(self, provider_id: str, generation: int) -> Mapping[str, object]:
        return self.connections.negotiated_limits(provider_id, generation)

    async def serve_callback(self, context, reader, writer) -> None:
        await self.connections.serve(context, reader, writer)

    def binding_projection(self, binding: TerminalBindingRecord) -> dict[str, object]:
        result = self.bindings.project(binding)
        provider_health = self.connections.health(binding.provider_id)
        if not self.connections.is_current(binding.provider_id, binding.provider_generation):
            result["health"] = "offline" if provider_health == "offline" else "reconciling"
        return result

    def heartbeat(self, provider_id: str, generation: int, report_revision: int) -> dict:
        self._accept_report_revision(provider_id, generation, report_revision)
        self.connections.renew(provider_id, generation)
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
    ) -> dict:
        if not self.connections.is_current(provider_id, generation):
            raise StaleGeneration(provider_id, generation)
        terminals = self._terminal_facts(facts)
        receipts = self._receipt_facts(facts)
        self._validate_receipts(provider_id, receipts)
        has_terminal_facts = facts is not None and "terminals" in facts
        inventory_verified = facts is not None and facts.get("complete") is True
        timestamp = self._clock()
        with self._store.write_unit() as unit:
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
            restored = (
                self.bindings.reconcile(
                    provider_id,
                    generation,
                    report_revision,
                    terminals,
                    timestamp=timestamp,
                    connection=unit.connection,
                )
                if has_terminal_facts
                else ()
            )
            record = self._store.providers.get(provider_id, connection=unit.connection)
            assert record is not None
            health = "online" if inventory_verified else self.connections.health(provider_id)
            first_revision = self._store.journal.current_sequence(connection=unit.connection) + 1
            events = [
                provider_event(
                    record,
                    health,
                    timestamp,
                    revision=first_revision,
                )
            ]
            for participant_id in restored:
                binding = self._store.terminal_bindings.get(
                    participant_id, connection=unit.connection
                )
                assert binding is not None
                events.append(
                    JournalEventRecord(
                        kind="terminal.binding_changed",
                        entity_id=participant_id,
                        entity_revision=first_revision + len(events),
                        payload=self.bindings.project(binding),
                        recorded_at=timestamp,
                    )
                )
            self._store.journal.append_group(unit, events)
            if inventory_verified:
                unit.after_commit(lambda: self.connections.mark_online(provider_id, generation))
            unit.after_commit(lambda: self.connections.renew(provider_id, generation))
        reconciled = self._reconcile_receipts(receipts)
        health = self.connections.health(provider_id)
        return {
            "provider_id": provider_id,
            "provider_generation": generation,
            "report_revision": report_revision,
            "health": health,
            "restored_participant_ids": list(restored),
            "reconciled_operation_ids": list(reconciled),
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
        self.report(
            provider_id,
            generation,
            revision,
            {"terminals": terminals, "complete": result["complete"]},
        )
        return result

    async def inspect(
        self, provider_id: str, generation: int, terminal_id: str, incarnation: str
    ) -> Mapping[str, object]:
        result = await self.connections.request(
            provider_id,
            generation,
            "terminal.inspect",
            {
                "provider_generation": generation,
                "terminal_id": terminal_id,
                "terminal_incarnation": incarnation,
            },
        )
        terminal = result["terminal"]
        revision = result["report_revision"]
        assert isinstance(terminal, Mapping) and isinstance(revision, int)
        self.report(
            provider_id,
            generation,
            revision,
            {"terminals": [terminal], "complete": False},
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
    ) -> None:
        for receipt in receipts:
            operation_id = receipt.get("operation_id")
            if not isinstance(operation_id, str):
                raise ProviderReportInvalid("provider receipts require an operation_id")
            operation = self._operations.get(operation_id)
            terminal = receipt.get("terminal")
            identity = terminal if isinstance(terminal, Mapping) else receipt
            expected = (
                operation.dispatch_provider_id,
                operation.dispatch_provider_generation,
                operation.dispatch_terminal_id,
                operation.dispatch_terminal_incarnation,
            )
            reported = (
                provider_id,
                identity.get("provider_generation"),
                identity.get("terminal_id"),
                identity.get("terminal_incarnation"),
            )
            if expected != reported:
                raise TerminalIdentityMismatch(
                    provider_id, str(identity.get("terminal_id")), "historical_receipt"
                )
            outcome = receipt.get("outcome", receipt.get("delivery"))
            if outcome not in {"accepted", "rejected", "unknown"}:
                raise ProviderReportInvalid(
                    "provider receipt outcome must be accepted, rejected, or unknown"
                )

    def _reconcile_receipts(self, receipts: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
        reconciled: list[str] = []
        for receipt in receipts:
            operation_id = str(receipt["operation_id"])
            operation = self._operations.get(operation_id)
            if operation.state not in {"running", "uncertain"}:
                continue
            outcome = receipt.get("outcome", receipt.get("delivery"))
            if outcome == "unknown":
                continue
            if outcome == "accepted":
                self._operations.succeed(
                    operation_id,
                    phase="provider_receipt_reconciled",
                    result=dict(receipt),
                )
            else:
                error = receipt.get("error")
                self._operations.fail(
                    operation_id,
                    phase="provider_receipt_reconciled",
                    error=(
                        dict(error)
                        if isinstance(error, Mapping)
                        and isinstance(error.get("code"), str)
                        and isinstance(error.get("message"), str)
                        else {
                            "code": "provider_unavailable",
                            "message": "provider receipt proves the terminal request was rejected",
                        }
                    ),
                )
            reconciled.append(operation_id)
        return tuple(reconciled)

    async def aclose(self) -> None:
        await self.connections.aclose()

    def _accept_report_revision(
        self, provider_id: str, generation: int, report_revision: int
    ) -> None:
        if not self.connections.is_current(provider_id, generation):
            raise StaleGeneration(provider_id, generation)
        with self._store.write_unit() as unit:
            if not self._store.providers.accept_report_revision(
                provider_id,
                generation=generation,
                report_revision=report_revision,
                updated_at=self._clock(),
                connection=unit.connection,
            ):
                self._raise_stale_report(
                    provider_id,
                    generation,
                    report_revision,
                    connection=unit.connection,
                )

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

    @staticmethod
    def _result_error(result: Mapping[str, object], fallback: str) -> dict[str, object]:
        error = result.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("code"), str):
            return dict(error)
        return {"code": "provider_unavailable", "message": fallback}


__all__ = ["ProviderReportInvalid", "StaleReportRevision", "TerminalProviderService"]
