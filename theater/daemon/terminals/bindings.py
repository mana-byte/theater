"""Exact terminal identity checks and current-generation binding recovery."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from jsonschema.exceptions import ValidationError

from theater.frontend.schemas import validator_for
from theater.models import Participant, Status, TerminalBindingRecord, TheaterError

_TERMINAL_IDENTITY_SCHEMA = (
    "https://theater.dev/schemas/frontend/1.0/common.json#/$defs/terminalIdentity"
)


class TerminalIdentityMismatch(TheaterError):
    code = "terminal_identity_mismatch"

    def __init__(self, provider_id: str, terminal_id: str, reason: str) -> None:
        self.details = {
            "provider_id": provider_id,
            "terminal_id": terminal_id,
            "reason": reason,
        }
        super().__init__(
            f"terminal {terminal_id!r} for provider {provider_id!r} failed its identity fence: "
            f"{reason}"
        )


class TerminalBindingService:
    def __init__(self, store) -> None:
        self._store = store

    def list(self, provider_id: str) -> tuple[TerminalBindingRecord, ...]:
        return self._store.terminal_bindings.list_for_provider(provider_id)

    def participants_on_other_tmux_server(
        self,
        provider_id: str,
        server_identity: str,
        *,
        connection,
    ) -> tuple[Participant, ...]:
        """Return live owners whose durable binding proves an older tmux epoch."""
        affected: list[Participant] = []
        for binding in self._store.terminal_bindings.list_for_provider(
            provider_id, connection=connection
        ):
            evidence = binding.occupant_evidence
            if (
                evidence.get("provider_kind") != "tmux"
                or not isinstance(evidence.get("tmux_server_identity"), str)
                or evidence["tmux_server_identity"] == server_identity
            ):
                continue
            participant = self._store.get_participant(binding.participant_id, connection=connection)
            if participant is not None and participant.status is not Status.DEAD:
                affected.append(participant)
        return tuple(affected)

    def reconcile(
        self,
        provider_id: str,
        generation: int,
        report_revision: int,
        terminals: Sequence[Mapping[str, object]],
        *,
        complete: bool,
        timestamp: float,
        connection,
        publish_refresh: bool = True,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        reported: dict[str, Mapping[str, object]] = {}
        for terminal in terminals:
            try:
                validator_for(_TERMINAL_IDENTITY_SCHEMA).validate(terminal)
            except ValidationError as exc:
                raise ValueError(f"provider terminal identity is invalid: {exc}") from exc
            terminal_id = str(terminal["terminal_id"])
            if terminal_id in reported:
                raise TerminalIdentityMismatch(provider_id, terminal_id, "duplicate_terminal_id")
            if (
                terminal["provider_id"] != provider_id
                or terminal["provider_generation"] != generation
            ):
                raise TerminalIdentityMismatch(provider_id, terminal_id, "provider_generation")
            reported[terminal_id] = terminal

        restored: list[str] = []
        changed: list[str] = []
        for binding in self._store.terminal_bindings.list_for_provider(
            provider_id, connection=connection
        ):
            owner = self._store.get_participant(binding.participant_id, connection=connection)
            if owner is None or owner.status is Status.DEAD:
                continue
            candidate = reported.get(binding.terminal_id)
            if candidate is None:
                if complete and binding.provider_generation == generation:
                    updated = self._store.terminal_bindings.update_health(
                        binding.participant_id,
                        provider_generation=generation,
                        report_revision=report_revision,
                        health="missing",
                        updated_at=timestamp,
                        connection=connection,
                    )
                    if updated and (publish_refresh or binding.health != "missing"):
                        changed.append(binding.participant_id)
                continue
            self._match(binding, candidate)
            if self._refresh_healthy(
                binding,
                generation=generation,
                report_revision=report_revision,
                timestamp=timestamp,
                connection=connection,
            ):
                restored.append(binding.participant_id)
                # Re-confirming an already healthy binding only advances its private
                # report revision; journaling it would publish every presence refresh.
                if publish_refresh or binding.health != "healthy":
                    changed.append(binding.participant_id)
        return tuple(restored), tuple(changed)

    def _refresh_healthy(
        self,
        binding: TerminalBindingRecord,
        *,
        generation: int,
        report_revision: int,
        timestamp: float,
        connection,
    ) -> bool:
        if binding.provider_generation == generation:
            return self._store.terminal_bindings.update_health(
                binding.participant_id,
                provider_generation=generation,
                report_revision=report_revision,
                health="healthy",
                updated_at=timestamp,
                connection=connection,
            )
        return self._store.terminal_bindings.restore_generation(
            binding.participant_id,
            previous_generation=binding.provider_generation,
            provider_generation=generation,
            report_revision=report_revision,
            health="healthy",
            updated_at=timestamp,
            connection=connection,
        )

    @staticmethod
    def project(binding: TerminalBindingRecord) -> dict[str, object]:
        return {
            "provider_id": binding.provider_id,
            "provider_generation": binding.provider_generation,
            "terminal_id": binding.terminal_id,
            "terminal_incarnation": binding.terminal_incarnation,
            "occupant": dict(binding.occupant_evidence),
            "process": None if binding.process_facts is None else dict(binding.process_facts),
            "participant_id": binding.participant_id,
            "health": binding.health,
            "report_revision": binding.report_revision,
        }

    @staticmethod
    def _match(binding: TerminalBindingRecord, terminal: Mapping[str, object]) -> None:
        if terminal["terminal_incarnation"] != binding.terminal_incarnation:
            raise TerminalIdentityMismatch(
                binding.provider_id, binding.terminal_id, "terminal_incarnation"
            )
        if terminal["occupant"] != binding.occupant_evidence:
            raise TerminalIdentityMismatch(binding.provider_id, binding.terminal_id, "occupant")
        process = terminal.get("process")
        if binding.process_facts is not None and process != binding.process_facts:
            raise TerminalIdentityMismatch(binding.provider_id, binding.terminal_id, "process")


def ensure_terminal_unbound(store, candidate: TerminalBindingRecord, connection) -> None:
    """Reject live ownership conflicts while allowing pane ids reused after exit."""
    for binding in store.terminal_bindings.list_for_provider(
        candidate.provider_id, connection=connection
    ):
        if binding.terminal_id != candidate.terminal_id:
            continue
        if binding.participant_id == candidate.participant_id:
            raise TerminalIdentityMismatch(
                candidate.provider_id, candidate.terminal_id, "participant_already_bound"
            )
        owner = store.get_participant(binding.participant_id, connection=connection)
        if (
            owner is None or owner.status is Status.DEAD
        ) and binding.terminal_incarnation != candidate.terminal_incarnation:
            continue
        reason = (
            "occupant_replaced"
            if binding.terminal_incarnation == candidate.terminal_incarnation
            and binding.occupant_evidence != candidate.occupant_evidence
            else "terminal_id_reused"
        )
        raise TerminalIdentityMismatch(candidate.provider_id, candidate.terminal_id, reason)


__all__ = ["TerminalBindingService", "TerminalIdentityMismatch", "ensure_terminal_unbound"]
