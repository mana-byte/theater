"""Fixtures for the handoff tests."""

from __future__ import annotations

import pytest

from theater.daemon.operations import OperationOutcome
from theater.daemon.spawning.models import Reservation
from theater.harness.base import LaunchPlan
from theater.models import ProviderRecord, now


@pytest.fixture
def launches(daemon, monkeypatch) -> list:
    """Fake provider: record each launch request instead of creating a terminal."""
    timestamp = now()
    record = ProviderRecord(
        provider_id="provider-tmux",
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
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(record, connection=unit.connection)
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")
    requests: list = []

    async def prepare(req, participant, *, child_cwd, provider, workspace_usage_id, **_):
        requests.append(req)
        return Reservation(
            participant=participant,
            plan=LaunchPlan(argv=["/bin/sh", "--fixture"]),
            child_cwd=child_cwd,
            session="",
            name="fixture",
            req=req,
            provider=provider,
            workspace_usage_id=workspace_usage_id,
        )

    async def dispatch(provider_id, generation, _method, params):
        pid = str(params["participant_id"])
        return OperationOutcome.succeeded(
            phase="provider_acknowledged",
            result={
                "operation_id": params["operation_id"],
                "provider_generation": generation,
                "outcome": "accepted",
                "terminal": {
                    "provider_id": provider_id,
                    "provider_generation": generation,
                    "terminal_id": f"terminal-{pid}",
                    "terminal_incarnation": "incarnation-1",
                    "occupant": {"occupant_id": pid, "harness": "codex"},
                    "process": {"pid": 1234, "started_at": 10.0, "executable": "/bin/agent"},
                },
            },
        )

    monkeypatch.setattr(daemon.spawner, "prepare_provider_launch", prepare)
    monkeypatch.setattr(daemon.terminal_service, "dispatch_operation", dispatch)
    return requests
