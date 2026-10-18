"""A provider-bound participant and the online-provider stand-ins its control tests use."""

from __future__ import annotations

import pytest

from tests.support.paths import fixture
from theater.models import (
    TerminalBindingRecord,
    now,
)


def _bind(daemon, participant_id: str, *, generation: int = 1, harness: str = "codex") -> None:
    timestamp = now()
    with daemon.store.write_unit() as unit:
        daemon.store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id=participant_id,
                provider_id="provider-a",
                provider_generation=generation,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                occupant_evidence={"occupant_id": "occupant-a", "harness": harness},
                process_facts={"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
                health="healthy",
                report_revision=1,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )


def provider_target(daemon, *, harness="codex") -> str:
    participant = daemon.registry.register(harness=harness, pane=None, cwd=None)
    _bind(daemon, participant.id, harness=harness)
    return participant.id


def online(monkeypatch: pytest.MonkeyPatch, daemon) -> None:
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")

    async def inspect(provider_id, generation, terminal_id, incarnation, *, screen_max_bytes=0):
        current = next(
            binding
            for binding in daemon.store.terminal_bindings.list_for_provider(provider_id)
            if binding.terminal_id == terminal_id
            and binding.terminal_incarnation == incarnation
            and binding.provider_generation == generation
        )
        revision = current.report_revision + 1
        with daemon.store.write_unit() as unit:
            assert daemon.store.terminal_bindings.update_health(
                current.participant_id,
                provider_generation=generation,
                report_revision=revision,
                health="healthy",
                updated_at=now(),
                connection=unit.connection,
            )
        # Provider sends require a proven ready composer; this fixture's
        # participants are idle codex terminals.
        idle_screen = fixture("screens", "codex_idle.txt")
        return {
            "provider_generation": generation,
            "report_revision": revision,
            "terminal": {
                "provider_id": provider_id,
                "provider_generation": generation,
                "terminal_id": terminal_id,
                "terminal_incarnation": incarnation,
                "occupant": dict(current.occupant_evidence),
                "process": dict(current.process_facts or {}),
            },
            "presence": {
                "state": "absent",
                "revision": revision,
                "reason": "test-no-human-focus",
            },
            "screen": (idle_screen.read_text() if screen_max_bytes else None),
        }

    monkeypatch.setattr(daemon.terminal_service, "inspect", inspect)
