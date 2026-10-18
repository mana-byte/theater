"""Provider-backed spawn and adoption: reservation, fencing, rollback, exact-identity adoption."""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select

from tests.support.provider_records import online_provider, register_provider
from tests.support.waiting import settle_operations
from theater.daemon.operations import OperationOutcome
from theater.daemon.persistence.repositories._json import decode_json
from theater.daemon.rpc.spawning import _spawn
from theater.daemon.schema import launch_reservations, orchestration_events, participants
from theater.daemon.spawning.models import Reservation
from theater.daemon.spawning.provider_launch import ParticipantLaunchService
from theater.daemon.terminals import ProviderUnavailable, TerminalIdentityMismatch
from theater.daemon.worktrees import identity as workspace_identity_module
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.schemas import validate_callback_request, validator_for
from theater.harness.base import LaunchPlan
from theater.models import BadRequest, JobState, Status, Tier
from theater.provenance import TranscriptProvenance

pytestmark = pytest.mark.usefixtures("available_harness_binaries")


async def test_missing_harness_binary_refuses_spawn_before_reserving_state(
    daemon, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("theater.daemon.spawning.provider_launch.shutil.which", lambda _: None)

    with pytest.raises(BadRequest, match="'codex' is not on PATH"):
        await ParticipantLaunchService(daemon).spawn(
            client_id="operator-a",
            idempotency_key="missing-binary",
            params={"harness": "codex", "approval": "manual", "cwd": str(tmp_path)},
        )

    assert daemon.store.operations.get_idempotency("operator-a", "missing-binary") is None
    assert daemon.store.conn.execute(select(func.count()).select_from(participants)).scalar() == 0


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
    register_provider(daemon, provider_id, selector=selector)


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
            plan=LaunchPlan(argv=["/bin/sh", "--fixture"]),
            child_cwd=child_cwd,
            session="",
            name="fixture",
            req=req,
            provider=provider,
            workspace_usage_id=workspace_usage_id,
        )

    monkeypatch.setattr(daemon.spawner, "prepare_provider_launch", prepare)


async def test_provider_launch_pins_daemon_resolved_executable(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    online_provider(daemon, monkeypatch)
    fixture_which = shutil.which
    resolved = "/opt/homebrew/bin/codex"

    def which(command, *args, **kwargs):
        return resolved if command == "codex" else fixture_which(command, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)

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
            plan=LaunchPlan(argv=["codex", "--fixture"]),
            child_cwd=child_cwd,
            session="",
            name="fixture",
            req=req,
            provider=provider,
            workspace_usage_id=workspace_usage_id,
        )

    monkeypatch.setattr(daemon.spawner, "prepare_provider_launch", prepare)
    dispatched: list[dict[str, object]] = []

    async def dispatch(provider_id, generation, _method, params):
        dispatched.append(dict(params))
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
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="operator-a",
        idempotency_key="resolved-executable",
        params={"harness": "codex", "approval": "manual", "cwd": str(tmp_path)},
    )
    await settle_operations(daemon)

    operation = daemon.operation_service.get(str(accepted["operation_id"]))
    launch = dispatched[0]["launch"]
    assert operation.state == "succeeded"
    assert isinstance(launch, dict)
    assert launch["executable"] == resolved
    assert launch["argv"] == [resolved, "--fixture"]


def test_public_spawn_request_defaults_an_omitted_prompt_to_empty() -> None:
    request = ParticipantLaunchService._spawn_request(
        {"harness": "codex", "approval": "manual"},
        parent_id=None,
        cwd="/workspace",
        worktree=False,
        base_ref=None,
    )

    assert request.prompt == ""


async def test_root_and_child_spawn_use_reserved_ids_and_handoff_workspace(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    online_provider(daemon, monkeypatch)
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
    await settle_operations(daemon)
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
    await settle_operations(daemon)

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
    await settle_operations(daemon)

    assert dispatched == [("provider-configured", 1)]
    assert accepted["operation_state"] == "succeeded"
    assert "operation_timed_out" not in accepted
    operation = daemon.operation_service.get(str(accepted["operation_id"]))
    assert operation.dispatch_provider_id == "provider-configured"


async def test_private_spawn_reports_unconfirmed_launch_without_replaying(
    daemon, monkeypatch, tmp_path
) -> None:
    online_provider(daemon, monkeypatch)
    _make_launch_preparation(monkeypatch, daemon)
    error = {"code": "terminal_create_failed", "message": "tmux creation failed"}
    calls = 0

    async def dispatch(*_args):
        nonlocal calls
        calls += 1
        return OperationOutcome.uncertain(phase="provider_outcome_unknown", error=error)

    wait = daemon.operation_service.wait

    async def wait_after_dispatch(operation_id):
        await settle_operations(daemon)
        return await wait(operation_id, wait_seconds=0)

    monkeypatch.setattr(daemon.terminal_service, "dispatch_operation", dispatch)
    monkeypatch.setattr(daemon.operation_service, "wait", wait_after_dispatch)
    request = {
        "harness": "codex",
        "prompt": "large handoff",
        "approval": "manual",
        "cwd": str(tmp_path),
        "idempotency_key": "private-uncertain",
    }
    result = await _spawn(daemon, request)
    assert result["operation_state"] == "uncertain"
    assert result["operation_timed_out"] is True
    assert result["operation_error"] == error
    assert "Do not spawn a replacement" in result["operation_warning"]
    assert "await_sessions([handle])" in result["operation_warning"]
    assert "job ends crashed if the launch fails" in result["operation_warning"]
    assert "list_participants(ids=[id])" in result["operation_warning"]
    assert result["handle"] == result["id"]
    assert result["addressable"] is False
    assert await _spawn(daemon, request) == result
    assert calls == 1


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
    online_provider(daemon, monkeypatch)

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
    await settle_operations(daemon)

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
    online_provider(daemon, monkeypatch)

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

    await settle_operations(daemon)

    workspace = daemon.store.workspaces.get(workspace.workspace_id)
    assert workspace is not None and workspace.state == "removed"
    assert not Path(workspace.path).exists()
    assert _git(repository, "branch", "--list", workspace.branch or "") == ""


async def test_spawn_resolves_git_before_acceptance_and_skips_it_for_replay(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    online_provider(daemon, monkeypatch)
    _make_launch_preparation(monkeypatch, daemon)

    async def reject(*_args, **_kwargs):
        return OperationOutcome.failed(
            phase="provider_rejected",
            error={"code": "provider_busy", "message": "fixture refusal"},
        )

    monkeypatch.setattr(daemon.terminal_service, "dispatch_operation", reject)
    original = workspace_identity_module.resolve_creation_facts
    calls: list[bool] = []
    main_thread = threading.get_ident()

    def guarded(*args, **kwargs):
        assert threading.get_ident() != main_thread
        calls.append(daemon.store._db._write_unit_active)
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace_identity_module, "resolve_creation_facts", guarded)
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
    await settle_operations(daemon)


async def test_definitive_create_rejection_rolls_back_after_persisting_target(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    online_provider(daemon, monkeypatch)
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
    await settle_operations(daemon)

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
    online_provider(daemon, monkeypatch)
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
    await settle_operations(daemon)
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


async def test_explicit_adoption_uses_fresh_inspect_and_binds_new_participant(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    online_provider(daemon, monkeypatch, "provider-a")
    inspected: list[tuple[str, int, str, str]] = []

    async def inspect(provider_id, generation, terminal_id, incarnation, *, screen_max_bytes=0):
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
    await settle_operations(daemon)

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
    online_provider(daemon, monkeypatch, "provider-a")
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

    async def inspect(*_args, **_kwargs):
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
    await settle_operations(daemon)

    operation = daemon.operation_service.get(str(accepted["operation_id"]))
    assert operation.state == "failed"
    assert operation.error_code == TerminalIdentityMismatch.code
    assert daemon.store.terminal_bindings.get(external.id) is None
    assert daemon.registry.get(external.id).status is Status.IDLE
    assert daemon.registry.get(external.id).termination_reason is None


def _replaced_incarnation_terminal() -> dict[str, object]:
    return {**_terminal(), "terminal_incarnation": "replacement-incarnation"}


@pytest.mark.parametrize(
    ("terminal", "lifecycle"),
    [
        pytest.param(_replaced_incarnation_terminal(), {"alive": True}, id="replaced_incarnation"),
        pytest.param(_terminal(), {"alive": False}, id="dead_occupant"),
    ],
)
async def test_adoption_of_a_new_participant_is_refused_and_retires_its_reservation(
    daemon, monkeypatch: pytest.MonkeyPatch, terminal: dict[str, object], lifecycle: dict
) -> None:
    online_provider(daemon, monkeypatch, "provider-a")

    async def inspect(*_args, **_kwargs):
        return {
            "provider_generation": 1,
            "report_revision": 2,
            "terminal": terminal,
            "presence": {"state": "absent", "revision": 1},
            "lifecycle": lifecycle,
        }

    monkeypatch.setattr(daemon.terminal_service, "inspect", inspect)
    accepted = ParticipantLaunchService(daemon).adopt(
        client_id="operator-a",
        idempotency_key="adopt-refused",
        params={
            "provider_id": "provider-a",
            "terminal_id": "terminal-a",
            "terminal_incarnation": "incarnation-a",
        },
    )
    await settle_operations(daemon)

    operation = daemon.operation_service.get(str(accepted["operation_id"]))
    participant = daemon.registry.get(str(accepted["participant_id"]))
    assert operation.state == "failed"
    assert operation.error_code == TerminalIdentityMismatch.code
    assert participant.status is Status.DEAD
    assert participant.termination_reason == "adoption_failed"
    assert daemon.store.terminal_bindings.get(participant.id) is None
