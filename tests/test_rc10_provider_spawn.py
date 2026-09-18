"""Focused RC10 provider-backed spawn acceptance and fencing checks."""

from __future__ import annotations

import asyncio
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select

from theater.daemon.operations import OperationOutcome
from theater.daemon.persistence.repositories._json import decode_json
from theater.daemon.rpc.spawning import _spawn
from theater.daemon.schema import launch_reservations, orchestration_events, participants
from theater.daemon.spawning.models import Reservation
from theater.daemon.spawning.provider_launch import ParticipantLaunchService
from theater.daemon.terminals import ProviderUnavailable
from theater.daemon.worktrees import service as workspace_service_module
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.schemas import validate_callback_request, validator_for
from theater.harness.base import LaunchPlan
from theater.models import BadRequest, JobState, ProviderRecord, Status, now


def _provider(provider_id: str, selector: str) -> ProviderRecord:
    timestamp = now()
    return ProviderRecord(
        provider_id=provider_id,
        selector=selector,
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


def _identity(provider_id: str, participant_id: str) -> dict[str, object]:
    return {
        "provider_id": provider_id,
        "provider_generation": 1,
        "terminal_id": f"terminal-{participant_id}",
        "terminal_incarnation": "incarnation-1",
        "occupant": {"occupant_id": participant_id, "harness": "codex"},
        "process": {"pid": 1234, "started_at": 10.0, "executable": "/bin/agent"},
    }


def _repository(path: Path) -> Path:
    root = path / "repository"
    root.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    (root / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)
    return root


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


def _install_provider(daemon, provider_id: str = "provider-tmux", selector: str = "tmux") -> None:
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(
            _provider(provider_id, selector), connection=unit.connection
        )


def _make_launch_preparation(monkeypatch, daemon) -> None:
    async def prepare(
        req,
        participant,
        *,
        child_cwd,
        provider,
        workspace_usage_id,
        **_kwargs,
    ):
        return Reservation(
            participant=participant,
            plan=LaunchPlan(argv=["/bin/agent", "--fixture"]),
            child_cwd=child_cwd,
            session="",
            name="fixture",
            req=req,
            provider=provider,
            workspace_usage_id=workspace_usage_id,
        )

    monkeypatch.setattr(daemon.spawner, "prepare_provider_launch", prepare)


async def _settle(daemon) -> None:
    await asyncio.sleep(0)
    tasks = daemon.operation_service.owned_tasks
    if tasks:
        await asyncio.gather(*tasks)


async def test_root_and_child_spawn_use_reserved_ids_and_handoff_workspace(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_provider(daemon)
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")
    _make_launch_preparation(monkeypatch, daemon)
    dispatched: list[dict[str, object]] = []

    async def dispatch(provider_id, generation, method, params):
        assert method == "terminal.create"
        validate_callback_request(
            {"type": "request", "id": "spawn-fixture", "method": method, "params": params}
        )
        dispatched.append(dict(params))
        daemon.registry.register(
            harness="codex",
            pane=None,
            cwd=str(tmp_path),
            claimed_id=str(params["participant_id"]),
        )
        return OperationOutcome.succeeded(
            phase="provider_acknowledged",
            result={
                "operation_id": params["operation_id"],
                "provider_generation": generation,
                "outcome": "accepted",
                "terminal": _identity(provider_id, str(params["participant_id"])),
            },
        )

    monkeypatch.setattr(daemon.terminal_service, "dispatch_operation", dispatch)
    service = ParticipantLaunchService(daemon)
    root = await service.spawn(
        client_id="operator-a",
        idempotency_key="spawn-root",
        params={
            "harness": "codex",
            "prompt": "root task",
            "approval": "yolo",
            "cwd": str(tmp_path),
        },
    )
    await _settle(daemon)
    child = await service.spawn(
        client_id="operator-a",
        idempotency_key="spawn-child",
        params={
            "harness": "codex",
            "prompt": "child task",
            "approval": "edits",
            "cwd": str(tmp_path),
            "initiating_participant_id": root["participant_id"],
        },
    )
    await _settle(daemon)

    for accepted in (root, child):
        validator_for(METHOD_CATALOG["frontend.participants.spawn"].result_schema_id).validate(
            accepted
        )
        operation = daemon.operation_service.get(str(accepted["operation_id"]))
        assert operation.state == "succeeded"
        binding = daemon.store.terminal_bindings.get(str(accepted["participant_id"]))
        assert binding is not None
        usage = daemon.store.workspaces.get_active_usage(
            daemon.registry.get(str(accepted["participant_id"])).workspace_id,
            holder_kind="participant",
            holder_id=str(accepted["participant_id"]),
        )
        assert usage is not None
    assert daemon.registry.get(str(child["participant_id"])).parent_id == root["participant_id"]
    assert len(dispatched) == 2
    count = daemon.store.conn.execute(select(func.count()).select_from(participants)).scalar_one()
    assert count == 2


async def test_private_spawn_uses_configured_default_provider(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_provider(daemon, "provider-configured", "configured-terminal")
    daemon.config = replace(
        daemon.config,
        terminals=replace(daemon.config.terminals, default_provider="configured-terminal"),
    )
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")
    _make_launch_preparation(monkeypatch, daemon)
    dispatched: list[tuple[str, int]] = []

    async def dispatch(provider_id, generation, _method, params):
        dispatched.append((provider_id, generation))
        return OperationOutcome.succeeded(
            phase="provider_acknowledged",
            result={
                "operation_id": params["operation_id"],
                "provider_generation": generation,
                "outcome": "accepted",
                "terminal": _identity(provider_id, str(params["participant_id"])),
            },
        )

    monkeypatch.setattr(daemon.terminal_service, "dispatch_operation", dispatch)
    accepted = await _spawn(
        daemon,
        {
            "harness": "codex",
            "prompt": "private default",
            "approval": "manual",
            "cwd": str(tmp_path),
        },
    )
    await _settle(daemon)

    assert dispatched == [("provider-configured", 1)]
    operation = daemon.operation_service.get(str(accepted["operation_id"]))
    assert operation.dispatch_provider_id == "provider-configured"


async def test_selected_provider_absence_and_no_failover(daemon, monkeypatch, tmp_path) -> None:
    _install_provider(daemon, "provider-a", "selected")
    _install_provider(daemon, "provider-b", "other")
    monkeypatch.setattr(
        daemon.terminal_service.connections,
        "is_current",
        lambda provider_id, _generation: provider_id == "provider-b",
    )
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")
    service = ParticipantLaunchService(daemon)

    with pytest.raises(ProviderUnavailable, match="provider-a"):
        await service.spawn(
            client_id="operator-a",
            idempotency_key="no-failover",
            params={
                "harness": "codex",
                "prompt": "task",
                "approval": "manual",
                "cwd": str(tmp_path),
                "provider": "selected",
            },
        )
    assert daemon.store.operations.get_idempotency("operator-a", "no-failover") is None

    with pytest.raises(ProviderUnavailable, match="missing"):
        await service.spawn(
            client_id="operator-a",
            idempotency_key="missing",
            params={
                "harness": "codex",
                "prompt": "task",
                "approval": "manual",
                "cwd": str(tmp_path),
                "provider": "missing",
            },
        )

    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "reconciling")
    with pytest.raises(ProviderUnavailable, match="not_launchable"):
        await service.spawn(
            client_id="operator-a",
            idempotency_key="reconciling",
            params={
                "harness": "codex",
                "prompt": "task",
                "approval": "manual",
                "cwd": str(tmp_path),
                "provider": "selected",
            },
        )
    assert daemon.store.operations.get_idempotency("operator-a", "reconciling") is None


async def test_pre_dispatch_failure_rolls_back_reserved_state(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_provider(daemon)
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")

    async def refuse_preparation(*_args, **_kwargs):
        raise BadRequest("launch plan cannot be built")

    monkeypatch.setattr(daemon.spawner, "prepare_provider_launch", refuse_preparation)
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="operator-a",
        idempotency_key="pre-dispatch-failure",
        params={
            "harness": "codex",
            "prompt": "task",
            "approval": "manual",
            "cwd": str(tmp_path),
        },
    )
    await _settle(daemon)

    operation = daemon.operation_service.get(str(accepted["operation_id"]))
    participant = daemon.registry.get(str(accepted["participant_id"]))
    job = daemon.store.get_job(str(accepted["job_handle"]))
    row = daemon.store.conn.execute(
        select(launch_reservations).where(
            launch_reservations.c.operation_id == accepted["operation_id"]
        )
    ).one()
    usage = daemon.store.workspaces.get_usage(row.workspace_usage_id)
    assert operation.state == "failed"
    assert operation.dispatch_provider_id is None
    assert participant.status is Status.DEAD
    assert participant.termination_reason == "spawn_failed"
    assert job is not None and job.state == JobState.CRASHED.value
    assert row.phase == "rolled_back" and row.dispatch_marker is None
    assert usage is not None and usage.release_reason == "launch_rolled_back"


async def test_pre_dispatch_failure_removes_only_its_durable_unique_workspace(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    _install_provider(daemon)
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")

    async def refuse_preparation(*_args, **_kwargs):
        raise BadRequest("launch plan cannot be built")

    monkeypatch.setattr(daemon.spawner, "prepare_provider_launch", refuse_preparation)
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="operator-a",
        idempotency_key="pre-dispatch-unique",
        params={
            "harness": "codex",
            "prompt": "task",
            "approval": "manual",
            "cwd": str(repository),
            "workspace": {"worktree": True},
        },
    )

    participant = daemon.registry.get(str(accepted["participant_id"]))
    workspace = daemon.store.workspaces.get(participant.workspace_id)
    launch = daemon.store.operations.get_launch(str(accepted["operation_id"]))
    assert workspace is not None and launch is not None
    assert workspace.state == "creating"
    assert workspace.creation_operation_id == accepted["operation_id"]
    assert launch.workspace_usage_id is not None
    assert workspace.resolved_base_commit == _git(repository, "rev-parse", "HEAD")

    await _settle(daemon)

    workspace = daemon.store.workspaces.get(workspace.workspace_id)
    assert workspace is not None and workspace.state == "removed"
    assert not Path(workspace.path).exists()
    assert _git(repository, "branch", "--list", workspace.branch or "") == ""


async def test_spawn_resolves_git_before_acceptance_and_skips_it_for_replay(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    _install_provider(daemon)
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")
    _make_launch_preparation(monkeypatch, daemon)

    async def reject(*_args, **_kwargs):
        return OperationOutcome.failed(
            phase="provider_rejected",
            error={"code": "provider_busy", "message": "fixture refusal"},
        )

    monkeypatch.setattr(daemon.terminal_service, "dispatch_operation", reject)
    original = workspace_service_module.resolve_creation_facts
    calls: list[bool] = []
    main_thread = threading.get_ident()

    def guarded(*args, **kwargs):
        assert threading.get_ident() != main_thread
        calls.append(daemon.store._db._write_unit_active)
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace_service_module, "resolve_creation_facts", guarded)
    params = {
        "harness": "codex",
        "prompt": "task",
        "approval": "manual",
        "cwd": str(repository),
        "workspace": {"worktree": True},
    }
    service = ParticipantLaunchService(daemon)
    accepted = await service.spawn(
        client_id="operator-a", idempotency_key="preflight-replay", params=params
    )
    replay = await service.spawn(
        client_id="operator-a", idempotency_key="preflight-replay", params=params
    )

    assert replay == accepted
    assert calls == [False]
    await _settle(daemon)


async def test_definitive_create_rejection_rolls_back_after_persisting_target(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_provider(daemon)
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")
    _make_launch_preparation(monkeypatch, daemon)
    observed_target: list[tuple[str | None, int | None]] = []

    async def reject(_provider_id, _generation, _method, params):
        operation = daemon.operation_service.get(str(params["operation_id"]))
        observed_target.append(
            (operation.dispatch_provider_id, operation.dispatch_provider_generation)
        )
        return OperationOutcome.failed(
            phase="provider_rejected",
            error={"code": "provider_busy", "message": "provider refused before execution"},
        )

    monkeypatch.setattr(daemon.terminal_service, "dispatch_operation", reject)
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="operator-a",
        idempotency_key="provider-rejected",
        params={
            "harness": "codex",
            "prompt": "task",
            "approval": "manual",
            "cwd": str(tmp_path),
        },
    )
    await _settle(daemon)

    operation = daemon.operation_service.get(str(accepted["operation_id"]))
    participant = daemon.registry.get(str(accepted["participant_id"]))
    job = daemon.store.get_job(str(accepted["job_handle"]))
    row = daemon.store.conn.execute(
        select(launch_reservations).where(
            launch_reservations.c.operation_id == accepted["operation_id"]
        )
    ).one()
    usage = daemon.store.workspaces.get_usage(row.workspace_usage_id)
    assert observed_target == [("provider-tmux", 1)]
    assert operation.state == "failed"
    assert participant.status is Status.DEAD
    assert job is not None and job.state == JobState.CRASHED.value
    assert usage is not None and usage.released_at is not None


async def test_lost_create_ack_stays_uncertain_and_retains_workspace(
    daemon, monkeypatch, tmp_path
) -> None:
    _install_provider(daemon)
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")
    _make_launch_preparation(monkeypatch, daemon)
    calls = 0

    async def dispatch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return OperationOutcome.uncertain(
            phase="provider_ack_lost",
            error={"code": "provider_unavailable", "message": "response lost"},
        )

    monkeypatch.setattr(daemon.terminal_service, "dispatch_operation", dispatch)
    service = ParticipantLaunchService(daemon)
    request = {
        "harness": "codex",
        "prompt": "task",
        "approval": "yolo",
        "cwd": str(tmp_path),
    }
    accepted = await service.spawn(
        client_id="operator-a",
        idempotency_key="lost-create",
        params=request,
    )
    await _settle(daemon)
    replay = await service.spawn(
        client_id="operator-a", idempotency_key="lost-create", params=request
    )

    operation = daemon.operation_service.get(str(accepted["operation_id"]))
    assert operation.state == "uncertain"
    assert operation.dispatch_provider_id == "provider-tmux"
    assert operation.dispatch_provider_generation == 1
    operation_events = daemon.store.conn.execute(
        select(orchestration_events.c.payload)
        .where(
            orchestration_events.c.kind == "operation.updated",
            orchestration_events.c.entity_id == accepted["operation_id"],
        )
        .order_by(orchestration_events.c.sequence)
    ).scalars()
    final_event = decode_json(list(operation_events)[-1])
    assert final_event["dispatch_identity"]["provider_id"] == "provider-tmux"
    assert final_event["dispatch_identity"]["provider_generation"] == 1
    assert final_event["dispatch_identity"]["terminal"] is None
    assert replay == accepted
    assert calls == 1
    row = daemon.store.conn.execute(
        select(launch_reservations).where(
            launch_reservations.c.operation_id == accepted["operation_id"]
        )
    ).one()
    assert row.dispatch_marker == "generation:1"
    assert row.provider_id == "provider-tmux"
    facts = decode_json(row.launch_facts)
    assert facts["provider_generation"] == 1
    assert facts["provider_selector"] == "tmux"
    assert facts["approval"] == "yolo"
    assert daemon.store.terminal_bindings.get(str(accepted["participant_id"])) is None
    assert daemon.store.workspaces.get_usage(row.workspace_usage_id).released_at is None
    assert daemon.registry.get(str(accepted["participant_id"])).status is Status.IDLE
