"""Public RC10 candidate flow through an independent provider process."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from test_rc10_provider_process import (
    _physical_events,
    _provider_process,
    _raw_register_provider,
)

from theater import paths
from theater.daemon.spawning.models import Reservation
from theater.frontend import FrontendClient, StateProjection, StateSynchronizer
from theater.harness.base import LaunchPlan
from theater.models import PublicOperationRecord, now


def _projection_wire(projection: StateProjection) -> dict[str, dict[str, object]]:
    return {
        "participants": {key: value.to_wire() for key, value in projection.participants.items()},
        "operations": {key: value.to_wire() for key, value in projection.operations.items()},
        "jobs": {key: value.to_wire() for key, value in projection.jobs.items()},
        "providers": {key: value.to_wire() for key, value in projection.providers.items()},
        "workspaces": {key: value.to_wire() for key, value in projection.workspaces.items()},
    }


def _install_test_launch_plan(daemon, monkeypatch: pytest.MonkeyPatch) -> None:
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
            plan=LaunchPlan(argv=["/bin/fixture-agent", "--exact", "value with spaces"]),
            child_cwd=child_cwd,
            session="",
            name=f"fixture-{participant.id[:6]}",
            req=req,
            provider=provider,
            workspace_usage_id=workspace_usage_id,
        )

    monkeypatch.setattr(daemon.spawner, "prepare_provider_launch", prepare)
    monkeypatch.setattr(
        "theater.daemon.spawning.provider_launch.shutil.which", lambda _binary: "/bin/true"
    )


async def _wait(client: FrontendClient, operation_id: str):
    result = await client.operations.wait(operation_id, wait_seconds=2)
    assert result.value.timed_out is False
    return result.value.operation


def _provider_generation(provider) -> int:
    generation = provider.ready["provider_generation"]
    assert type(generation) is int
    return generation


def _physical_occupant_id(event: Mapping[str, object]) -> str:
    terminal = event["terminal"]
    assert isinstance(terminal, Mapping)
    occupant = terminal["occupant"]
    assert isinstance(occupant, Mapping)
    occupant_id = occupant["occupant_id"]
    assert isinstance(occupant_id, str)
    return occupant_id


async def test_independent_provider_root_child_adoption_workspace_and_state_flow(  # noqa: PLR0915
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential = "s13-provider-credential"
    provider_id = await _raw_register_provider(paths.socket_path(), credential)
    plan = {
        "bind_requested_participant": True,
        "replace_occupant_after": {"terminal.inspect": "replacement-occupant"},
    }
    _install_test_launch_plan(daemon, monkeypatch)

    async with _provider_process(
        paths.socket_path(),
        provider_id=provider_id,
        provider_credential=credential,
        plan=plan,
    ) as provider:
        client = FrontendClient(paths.socket_path(), client_id="s13-operator")
        synchronizer = StateSynchronizer(client)
        try:
            await client.providers.terminals.list(provider_id, refresh=True)
            workspace_path = tmp_path / "retained-workspace"
            workspace_path.mkdir()
            workspace = (
                await client.workspaces.register(
                    "borrowed",
                    "s13-operator",
                    str(workspace_path),
                    idempotency_key="workspace-retained",
                )
            ).value

            root = (
                await client.participants.spawn(
                    "codex",
                    "root task",
                    "manual",
                    provider=provider_id,
                    workspace={"workspace_id": workspace.workspace_id},
                    idempotency_key="spawn-root",
                )
            ).value
            root_operation = await _wait(client, root.operation_id)
            assert root_operation.state == "succeeded"
            assert root.participant_id is not None

            child = (
                await client.participants.spawn(
                    "codex",
                    "child task",
                    "manual",
                    provider=provider_id,
                    workspace={"workspace_id": workspace.workspace_id},
                    initiating_participant_id=root.participant_id,
                    idempotency_key="spawn-child",
                )
            ).value
            child_operation = await _wait(client, child.operation_id)
            assert child_operation.state == "succeeded"
            assert child.participant_id is not None
            assert daemon.registry.get(child.participant_id).parent_id == root.participant_id

            creates = [
                event
                for event in _physical_events(provider)
                if event["method"] == "terminal.create"
            ]
            assert len(creates) == 2
            assert {_physical_occupant_id(event) for event in creates} == {
                root.participant_id,
                child.participant_id,
            }
            active = daemon.store.workspaces.active_usages(workspace.workspace_id)
            assert {usage.holder_id for usage in active} == {
                root.participant_id,
                child.participant_id,
            }

            external = daemon.registry.register(
                harness="codex",
                pane=None,
                cwd=str(tmp_path),
                claimed_id="external-participant",
            )
            timestamp = now()
            generation = _provider_generation(provider)
            with daemon.store.write_unit() as unit:
                daemon.store.operations.create(
                    PublicOperationRecord(
                        operation_id="external-terminal-create",
                        kind="fixture.create",
                        actor_client_id="s13-fixture",
                        actor_participant_id=None,
                        target_ids=(external.id,),
                        state="running",
                        phase="dispatching",
                        dispatch_provider_id=provider_id,
                        dispatch_provider_generation=generation,
                        created_at=timestamp,
                        updated_at=timestamp,
                    ),
                    connection=unit.connection,
                )
            created = await daemon.terminal_service.connections.request(
                provider_id,
                generation,
                "terminal.create",
                {
                    "operation_id": "external-terminal-create",
                    "provider_generation": generation,
                    "participant_id": external.id,
                    "launch_id": "external-launch",
                    "launch": {
                        "executable": "/bin/fixture-agent",
                        "argv": ["/bin/fixture-agent"],
                        "cwd": str(tmp_path),
                        "environment": {},
                    },
                },
            )
            terminal = created["terminal"]
            assert isinstance(terminal, Mapping)
            adopted = (
                await client.participants.adopt(
                    provider_id,
                    str(terminal["terminal_id"]),
                    str(terminal["terminal_incarnation"]),
                    participant_id=external.id,
                    idempotency_key="adopt-external",
                )
            ).value
            assert (await _wait(client, adopted.operation_id)).state == "succeeded"
            assert daemon.registry.get(external.id).origin.value == "external"

            rejected = (
                await client.controls.send(
                    external.id,
                    "must not reach replacement",
                    idempotency_key="stale-occupant-send",
                )
            ).value
            assert (await _wait(client, rejected.operation_id)).state == "failed"
            assert not any(
                event["method"] == "terminal.deliver" for event in _physical_events(provider)
            )

            transferred = (
                await client.participants.transfer_control(
                    [{"participant_id": child.participant_id, "expected_revision": 0}],
                    {"kind": "local_operator", "participant_id": None},
                    idempotency_key="transfer-child",
                )
            ).value
            changed = transferred["participants"]
            assert isinstance(changed, tuple)
            assert len(changed) == 1 and isinstance(changed[0], Mapping)
            assert changed[0]["participant_id"] == child.participant_id
            persisted_child = daemon.registry.get(child.participant_id)
            assert persisted_child.parent_id == root.participant_id
            assert persisted_child.control_owner_id is None

            disposable_path = tmp_path / "frontend-owned"
            disposable_path.mkdir()
            disposable = (
                await client.workspaces.register(
                    "frontend",
                    "s13-operator",
                    str(disposable_path),
                    idempotency_key="workspace-disposable",
                )
            ).value
            prepared = (
                await client.workspaces.prepare_delete(
                    disposable.workspace_id, idempotency_key="prepare-delete"
                )
            ).value
            token = prepared["token"]
            assert isinstance(token, str)
            deleted = (
                await client.workspaces.confirm_delete(
                    disposable.workspace_id,
                    token,
                    idempotency_key="confirm-delete",
                )
            ).value
            deleted_workspace = deleted["workspace"]
            assert isinstance(deleted_workspace, Mapping)
            assert deleted_workspace["state"] == "removed"
            assert disposable_path.exists()

            before = await synchronizer.refresh()
            await client.participants.update(
                root.participant_id,
                description="updated through public API",
                idempotency_key="update-root",
            )
            followed = await synchronizer.follow_once(wait_seconds=0)
            assert followed.cursor.sequence > before.cursor.sequence
            expected = await synchronizer.refresh()
            assert followed.cursor == expected.cursor
            assert _projection_wire(followed) == _projection_wire(expected)
        finally:
            await client.close()


async def test_lost_create_ack_keeps_one_side_effect_and_reserved_resources(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential = "s13-lost-ack-credential"
    provider_id = await _raw_register_provider(paths.socket_path(), credential)
    _install_test_launch_plan(daemon, monkeypatch)

    async with _provider_process(
        paths.socket_path(),
        provider_id=provider_id,
        provider_credential=credential,
        plan={
            "bind_requested_participant": True,
            "disconnect_after": ["terminal.create"],
        },
    ) as provider:
        client = FrontendClient(paths.socket_path(), client_id="s13-lost-ack")
        try:
            await client.providers.terminals.list(provider_id, refresh=True)
            workspace_path = tmp_path / "held-workspace"
            workspace_path.mkdir()
            workspace = (
                await client.workspaces.register(
                    "borrowed",
                    "s13-lost-ack",
                    str(workspace_path),
                    idempotency_key="lost-workspace",
                )
            ).value
            accepted = (
                await client.participants.spawn(
                    "codex",
                    "execute once",
                    "manual",
                    provider=provider_id,
                    workspace={"workspace_id": workspace.workspace_id},
                    idempotency_key="lost-create",
                )
            ).value
            observed = (await client.operations.wait(accepted.operation_id, wait_seconds=0.2)).value
            assert observed.timed_out is True
            assert observed.operation.state == "uncertain"
            assert await provider.wait() == 0
            assert [
                event["method"]
                for event in _physical_events(provider)
                if event["operation_id"] == accepted.operation_id
            ] == ["terminal.create"]

            replay = (
                await client.participants.spawn(
                    "codex",
                    "execute once",
                    "manual",
                    provider=provider_id,
                    workspace={"workspace_id": workspace.workspace_id},
                    idempotency_key="lost-create",
                )
            ).value
            assert replay.operation_id == accepted.operation_id
            launch = daemon.store.operations.get_launch(accepted.operation_id)
            assert launch is not None and launch.dispatch_marker is not None
            assert launch.workspace_usage_id is not None
            usage = daemon.store.workspaces.get_usage(launch.workspace_usage_id)
            assert usage is not None and usage.released_at is None
            assert accepted.job_handle is not None
            job = daemon.store.get_job(accepted.job_handle)
            assert job is not None and job.state == "running"
        finally:
            await client.close()


async def test_lost_input_ack_retains_barrier_without_duplicate_delivery(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential = "s13-lost-input-credential"
    provider_id = await _raw_register_provider(paths.socket_path(), credential)
    _install_test_launch_plan(daemon, monkeypatch)

    async with _provider_process(
        paths.socket_path(),
        provider_id=provider_id,
        provider_credential=credential,
        plan={
            "bind_requested_participant": True,
            "disconnect_after": ["terminal.deliver"],
        },
    ) as provider:
        client = FrontendClient(paths.socket_path(), client_id="s13-lost-input")
        try:
            await client.providers.terminals.list(provider_id, refresh=True)
            accepted = (
                await client.participants.spawn(
                    "codex",
                    "bootstrap",
                    "manual",
                    cwd=str(tmp_path),
                    provider=provider_id,
                    idempotency_key="input-target",
                )
            ).value
            assert (await _wait(client, accepted.operation_id)).state == "succeeded"
            assert accepted.participant_id is not None
            assert accepted.job_handle is not None
            daemon.jobs.finish(accepted.job_handle, state="done", result="ready")

            sent = (
                await client.controls.send(
                    accepted.participant_id,
                    "execute exactly once",
                    idempotency_key="lost-input",
                )
            ).value
            observed = (await client.operations.wait(sent.operation_id, wait_seconds=0.2)).value
            assert observed.timed_out is True
            assert observed.operation.state == "uncertain"
            assert await provider.wait() == 0

            replay = (
                await client.controls.send(
                    accepted.participant_id,
                    "execute exactly once",
                    idempotency_key="lost-input",
                )
            ).value
            assert replay.operation_id == sent.operation_id
            public = daemon.operation_service.get(sent.operation_id)
            assert public.control_operation_id is not None
            control = daemon.store.get_control_operation(public.control_operation_id)
            assert control is not None
            assert control.execution_barrier is True
            assert control.delivery_result.value == "unknown"
            assert public.job_handle is not None
            job = daemon.store.get_job(public.job_handle)
            assert job is not None and job.state == "running"
            assert [
                event["method"]
                for event in _physical_events(provider)
                if event["operation_id"] == sent.operation_id
            ] == ["terminal.deliver"]
        finally:
            await client.close()
