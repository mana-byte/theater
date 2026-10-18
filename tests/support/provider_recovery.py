"""Provider records, services and terminal identities for recovery tests."""

from __future__ import annotations

from theater.daemon.operations import OperationService
from theater.daemon.persistence.store import Store
from theater.daemon.plugins.credentials import credential_verifier
from theater.daemon.terminals import (
    TerminalProviderService,
)
from theater.models import (
    ProviderRecord,
)


def provider_record(*, kind: str = "test") -> ProviderRecord:
    return ProviderRecord(
        provider_id="provider-a",
        selector="fixture",
        kind=kind,
        credential_verifier=credential_verifier("credential-a"),
        configuration_version=1,
        capabilities=("terminal-provider.v1",),
        limits={},
        generation=0,
        last_report_revision=None,
        created_at=1.0,
        updated_at=1.0,
    )


def provider_service(store: Store) -> TerminalProviderService:
    return TerminalProviderService(store, OperationService(store))


def terminal_identity(generation: int, *, launch_id: str | None = None) -> dict[str, object]:
    identity: dict[str, object] = {
        "provider_id": "provider-a",
        "provider_generation": generation,
        "terminal_id": "terminal-a",
        "terminal_incarnation": "incarnation-a",
        "occupant": {"occupant_id": "participant-a", "harness": "codex"},
        "process": {"pid": 42, "started_at": 2.0, "executable": "/bin/agent"},
    }
    if launch_id is not None:
        identity["launch_id"] = launch_id
    return identity


def tmux_terminal_identity(
    generation: int,
    *,
    server_identity: str,
    participant_id: str,
    incarnation: str,
) -> dict[str, object]:
    return {
        "provider_id": "provider-a",
        "provider_generation": generation,
        "terminal_id": "%1",
        "terminal_incarnation": incarnation,
        "occupant": {
            "occupant_id": participant_id,
            "provider_kind": "tmux",
            "tmux_server_identity": server_identity,
            "terminal_incarnation": incarnation,
            "pane_pid": 42,
        },
        "process": {"pid": 42},
    }
