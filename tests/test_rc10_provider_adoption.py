"""Focused fresh-inspection and exact-identity adoption checks."""

from __future__ import annotations

import asyncio

import pytest

from theater.daemon.spawning.service import ParticipantLaunchService
from theater.daemon.terminals import TerminalIdentityMismatch
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.schemas import validator_for
from theater.models import ProviderRecord, Status, Tier, now
from theater.provenance import TranscriptProvenance


def _provider() -> ProviderRecord:
    timestamp = now()
    return ProviderRecord(
        provider_id="provider-a",
        selector="tmux",
        kind="fixture",
        credential_verifier="a" * 64,
        configuration_version=1,
        capabilities=("terminal-provider.v1",),
        limits={},
        generation=1,
        last_report_revision=1,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _terminal(*, occupant: str = "occupant-a", pid: int = 42) -> dict[str, object]:
    return {
        "provider_id": "provider-a",
        "provider_generation": 1,
        "terminal_id": "terminal-a",
        "terminal_incarnation": "incarnation-a",
        "occupant": {
            "occupant_id": occupant,
            "harness": "codex",
            "cwd": "/tmp/adopted",
        },
        "process": {"pid": pid, "started_at": 10.0, "executable": "/bin/codex"},
    }


def _ready(daemon, monkeypatch: pytest.MonkeyPatch) -> None:
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(_provider(), connection=unit.connection)
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")


async def _settle(daemon) -> None:
    await asyncio.sleep(0)
    tasks = daemon.operation_service.owned_tasks
    if tasks:
        await asyncio.gather(*tasks)


async def test_explicit_adoption_uses_fresh_inspect_and_binds_new_participant(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(daemon, monkeypatch)
    inspected: list[tuple[str, int, str, str]] = []

    async def inspect(provider_id, generation, terminal_id, incarnation):
        inspected.append((provider_id, generation, terminal_id, incarnation))
        return {
            "provider_generation": generation,
            "report_revision": 2,
            "terminal": _terminal(),
            "presence": {"state": "absent", "revision": 1},
            "lifecycle": {"alive": True},
        }

    monkeypatch.setattr(daemon.terminal_service, "inspect", inspect)
    accepted = ParticipantLaunchService(daemon).adopt(
        client_id="operator-a",
        idempotency_key="adopt-a",
        params={
            "provider_id": "provider-a",
            "terminal_id": "terminal-a",
            "terminal_incarnation": "incarnation-a",
        },
    )
    validator_for(METHOD_CATALOG["frontend.participants.adopt"].result_schema_id).validate(accepted)
    await _settle(daemon)

    participant_id = str(accepted["participant_id"])
    participant = daemon.registry.get(participant_id)
    binding = daemon.store.terminal_bindings.get(participant_id)
    assert inspected == [("provider-a", 1, "terminal-a", "incarnation-a")]
    assert participant.tier is Tier.ADOPTED
    assert participant.origin.value == "adopted"
    assert participant.harness == "codex"
    assert participant.cwd == "/tmp/adopted"
    assert binding is not None and binding.occupant_evidence["occupant_id"] == "occupant-a"
    operation = daemon.operation_service.get(str(accepted["operation_id"]))
    assert operation.state == "succeeded"
    assert operation.dispatch_terminal_incarnation == "incarnation-a"


async def test_adoption_refuses_replaced_process_for_existing_external(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(daemon, monkeypatch)
    external = daemon.registry.register(
        harness="codex",
        pane=None,
        pane_pid=41,
        cwd="/tmp/adopted",
        session_id="trusted-session",
    )
    external.session_correlation = str(TranscriptProvenance.EXACT)
    external.pid = 41
    daemon.store.upsert_participant(external)

    async def inspect(*_args):
        return {
            "provider_generation": 1,
            "report_revision": 2,
            "terminal": _terminal(pid=42),
            "presence": {"state": "absent", "revision": 1},
            "lifecycle": {"alive": True, "session_id": "trusted-session"},
        }

    monkeypatch.setattr(daemon.terminal_service, "inspect", inspect)
    accepted = ParticipantLaunchService(daemon).adopt(
        client_id="operator-a",
        idempotency_key="adopt-replaced",
        params={
            "provider_id": "provider-a",
            "terminal_id": "terminal-a",
            "terminal_incarnation": "incarnation-a",
            "participant_id": external.id,
        },
    )
    await _settle(daemon)

    operation = daemon.operation_service.get(str(accepted["operation_id"]))
    assert operation.state == "failed"
    assert operation.error_code == TerminalIdentityMismatch.code
    assert daemon.store.terminal_bindings.get(external.id) is None
    assert daemon.registry.get(external.id).status is Status.IDLE


async def test_adoption_refuses_inspection_of_replaced_incarnation(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(daemon, monkeypatch)

    async def inspect(*_args):
        terminal = _terminal()
        terminal["terminal_incarnation"] = "replacement-incarnation"
        return {
            "provider_generation": 1,
            "report_revision": 2,
            "terminal": terminal,
            "presence": {"state": "absent", "revision": 1},
            "lifecycle": {"alive": True},
        }

    monkeypatch.setattr(daemon.terminal_service, "inspect", inspect)
    accepted = ParticipantLaunchService(daemon).adopt(
        client_id="operator-a",
        idempotency_key="adopt-stale",
        params={
            "provider_id": "provider-a",
            "terminal_id": "terminal-a",
            "terminal_incarnation": "incarnation-a",
        },
    )
    await _settle(daemon)
    operation = daemon.operation_service.get(str(accepted["operation_id"]))
    assert operation.state == "failed"
    assert operation.error_code == "terminal_identity_mismatch"
