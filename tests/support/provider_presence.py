"""A scripted terminal service, clock and registry for provider presence tests."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from theater.models import (
    Participant,
    TerminalBindingRecord,
)


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class Bindings:
    def __init__(self, binding: TerminalBindingRecord) -> None:
        self.value = binding

    def get(self, participant_id: str) -> TerminalBindingRecord | None:
        return self.value if self.value.participant_id == participant_id else None


class Registry:
    def __init__(self, participant: Participant, binding: TerminalBindingRecord) -> None:
        self.participant = participant
        self.store = SimpleNamespace(terminal_bindings=Bindings(binding))

    def get(self, participant_id: str) -> Participant | None:
        return self.participant if self.participant.id == participant_id else None

    def list(self, **_kwargs) -> list[Participant]:
        return [self.participant]


class Connections:
    def __init__(self) -> None:
        self.generation = 7
        self.state = "online"

    def is_current(self, provider_id: str, generation: int) -> bool:
        return provider_id == "provider-a" and generation == self.generation

    def current_generation(self, provider_id: str) -> int | None:
        return self.generation if provider_id == "provider-a" else None

    def health(self, provider_id: str) -> str:
        assert provider_id == "provider-a"
        return self.state


class TerminalService:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.connections = Connections()
        self.responses: list[dict | Exception] = []
        self.inspect_calls = 0

    async def inspect(
        self,
        provider_id: str,
        generation: int,
        terminal_id: str,
        incarnation: str,
        *,
        screen_max_bytes: int = 0,
    ) -> dict:
        self.inspect_calls += 1
        assert (provider_id, generation, terminal_id, incarnation) == (
            "provider-a",
            self.connections.generation,
            "terminal-a",
            "incarnation-a",
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        revision = response["report_revision"]
        self.registry.store.terminal_bindings.value = replace(
            self.registry.store.terminal_bindings.value,
            report_revision=revision,
            provider_generation=generation,
            health="healthy",
        )
        return response


def binding(*, health: str = "healthy") -> TerminalBindingRecord:
    return TerminalBindingRecord(
        participant_id="participant-a",
        provider_id="provider-a",
        provider_generation=7,
        terminal_id="terminal-a",
        terminal_incarnation="incarnation-a",
        occupant_evidence={"occupant_id": "occupant-a"},
        process_facts={"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
        health=health,
        report_revision=0,
        created_at=1.0,
        updated_at=1.0,
    )


def result(
    state: str,
    revision: int,
    *,
    occupant: str = "occupant-a",
    lifecycle: dict | None = None,
) -> dict:
    value = {
        "provider_generation": 7,
        "report_revision": revision,
        "terminal": {
            "provider_id": "provider-a",
            "provider_generation": 7,
            "terminal_id": "terminal-a",
            "terminal_incarnation": "incarnation-a",
            "occupant": {"occupant_id": occupant},
            "process": {"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
        },
        "presence": {"state": state, "revision": revision, "reason": f"focus-{state}"},
        "screen": f"screen-{revision}",
    }
    if lifecycle is not None:
        value["lifecycle"] = lifecycle
    return value
