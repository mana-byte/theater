"""Wave-12 private CLI adapters share the public services, not a second projection."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from theater.daemon.frontend import provider_handlers, workspace_handlers
from theater.daemon.rpc import management, spawning
from theater.daemon.terminals.service import TerminalProviderService
from theater.daemon.worktrees.service import WorkspaceService
from theater.harness.contracts.runtime import RuntimeWiring
from theater.models import Status


class _Registry:
    def list(self, *, cursor: str | None, limit: int):
        assert (cursor, limit) == (None, 2)
        return (("provider-a",), None)

    def get(self, provider_id: str):
        assert provider_id == "provider-a"
        return provider_id

    def project(self, record: str) -> dict[str, object]:
        return {"provider_id": record, "selector": "tmux", "health": "online"}


class _TerminalService(TerminalProviderService):
    def __init__(self) -> None:
        self.registry = _Registry()


class _WorkspaceService(WorkspaceService):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def list(self, *, cursor: str | None, limit: int, state: str | None):
        assert (cursor, limit, state) == (None, 2, None)
        return (("workspace-a",), None)

    def get(self, workspace_id: str) -> str:
        assert workspace_id == "workspace-a"
        return workspace_id

    def project(self, record: str) -> dict[str, object]:
        return {"workspace_id": record, "state": "active"}

    def cleanup(
        self,
        *,
        client_id: str,
        actor_participant_id: str | None,
        idempotency_key: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append(
            {
                "client_id": client_id,
                "actor_participant_id": actor_participant_id,
                "idempotency_key": idempotency_key,
                "params": params,
            }
        )
        return {"operation_id": "op-cleanup", "state": "accepted"}


class _Daemon:
    def __init__(self) -> None:
        self.terminal_service = _TerminalService()
        self.workspace_service = _WorkspaceService()


async def test_private_provider_and_workspace_reads_match_public_projections() -> None:
    daemon = _Daemon()
    context = SimpleNamespace(client_id="public-client")

    assert await management.providers_list(
        daemon, {"limit": 2}
    ) == await provider_handlers.providers_list(daemon, context, {"limit": 2})
    assert await management.providers_get(
        daemon, {"provider_id": "provider-a"}
    ) == await provider_handlers.providers_get(daemon, context, {"provider_id": "provider-a"})
    assert await management.workspaces_list(
        daemon, {"limit": 2}
    ) == await workspace_handlers.workspaces_list(daemon, context, {"limit": 2})
    assert await management.workspaces_get(
        daemon, {"workspace_id": "workspace-a"}
    ) == await workspace_handlers.workspaces_get(daemon, context, {"workspace_id": "workspace-a"})


async def test_private_workspace_cleanup_preserves_the_public_service_request() -> None:
    daemon = _Daemon()
    params = {
        "workspace_id": "workspace-a",
        "force": True,
        "delete_branch": False,
        "force_branch": False,
    }

    private = await management.workspaces_cleanup(
        daemon, {**params, "idempotency_key": "cleanup-private"}
    )
    public = await workspace_handlers.workspaces_cleanup(
        daemon,
        SimpleNamespace(client_id="public-client"),
        params,
        idempotency_key="cleanup-public",
    )

    assert private == public == {"operation_id": "op-cleanup", "state": "accepted"}
    assert [call["params"] for call in daemon.workspace_service.calls] == [params, params]


async def test_private_control_transfer_uses_the_catalogued_idempotency_method(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class Transfer:
        def __init__(self, _daemon: object) -> None:
            pass

        def transfer(self, participants, owner, *, unit):
            assert unit is not None
            return {"participants": participants, "owner": owner}

    class Operations:
        def execute_idempotent(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(value=kwargs["action"](object()))

    class Controls:
        @asynccontextmanager
        async def hold_participant_locks(self, _participant_ids):
            yield

    daemon = SimpleNamespace(controls=Controls(), operation_service=Operations())
    monkeypatch.setattr(management, "ControlTransferService", Transfer)
    params = {
        "participants": [{"participant_id": "p-a", "expected_revision": 3}],
        "new_owner": {"kind": "local_operator", "participant_id": None},
        "idempotency_key": "transfer-key",
    }

    assert await management.controls_transfer(daemon, params) == {
        "participants": [{"participant_id": "p-a", "expected_revision": 3}],
        "owner": {"kind": "local_operator", "participant_id": None},
    }
    assert calls[0]["method"] == "frontend.participants.transfer_control"
    assert calls[0]["params"] == {key: params[key] for key in ("participants", "new_owner")}


async def test_private_spawn_provider_override_adapts_to_the_shared_launch_service(
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    class LaunchService:
        def spawn(self, **kwargs):
            calls.append(kwargs)
            return {
                "participant_id": "participant-a",
                "operation_id": "operation-a",
                "state": "accepted",
                "job_handle": "job-a",
            }

    class Registry:
        @staticmethod
        def get(participant_id: str):
            assert participant_id == "participant-a"
            return SimpleNamespace(
                status=Status.IDLE,
                to_dict=lambda: {"id": participant_id, "harness": "vibe"},
            )

    class Operations:
        @staticmethod
        async def wait(operation_id: str):
            assert operation_id == "operation-a"
            return SimpleNamespace(state="succeeded", error=None), False

    class Controls:
        @staticmethod
        def route_for(participant_id: str, _capability):
            assert participant_id == "participant-a"
            return SimpleNamespace(route_available=True)

    class Config:
        rails = SimpleNamespace(depth_cap=8, budget=8)

        @staticmethod
        def models_for(_harness: str):
            return None

        @staticmethod
        def reasoning_for(_harness: str):
            return None

    launch = LaunchService()
    daemon = SimpleNamespace(
        config=Config(),
        controls=Controls(),
        operation_service=Operations(),
        registry=Registry(),
        store=SimpleNamespace(),
    )
    monkeypatch.setattr(spawning, "ParticipantLaunchService", lambda _daemon: launch)
    monkeypatch.setattr(spawning, "check_depth", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(spawning, "check_budget", lambda *_args, **_kwargs: None)

    result = await spawning._spawn(
        daemon,
        {
            "harness": "vibe",
            "prompt": "work",
            "approval": "manual",
            "cwd": "/workspace",
            "provider": "provider-remote",
            "worktree": False,
            "base_branch": None,
            "parent_id": "parent-a",
            "idempotency_key": "spawn-key",
        },
    )

    assert result == {
        "id": "participant-a",
        "harness": "vibe",
        "addressable": True,
        "handle": "job-a",
        "operation_id": "operation-a",
        "operation_state": "accepted",
    }
    assert calls == [
        {
            "client_id": "private-rpc",
            "idempotency_key": "spawn-key",
            "params": {
                "harness": "vibe",
                "prompt": "work",
                "approval": "manual",
                "provider": "provider-remote",
                "workspace": {"cwd": "/workspace", "worktree": False, "base_ref": None},
                "initiating_participant_id": "parent-a",
                "model": None,
                "reasoning_effort": None,
                "resume": None,
                "name": None,
                "description": None,
            },
            "launch_prompt": "work",
            "launch_wiring": RuntimeWiring.AUTO,
            "launch_response_format": None,
        }
    ]


async def test_private_spawn_timeout_returns_the_correlated_accepted_operation(monkeypatch) -> None:
    class LaunchService:
        @staticmethod
        def spawn(**_kwargs):
            return {
                "participant_id": "participant-a",
                "operation_id": "operation-a",
                "state": "accepted",
                "job_handle": "job-a",
            }

    participant = SimpleNamespace(
        status=Status.IDLE,
        to_dict=lambda: {"id": "participant-a", "harness": "vibe"},
    )
    config = SimpleNamespace(
        rails=SimpleNamespace(depth_cap=8, budget=8),
        models_for=lambda _harness: None,
        reasoning_for=lambda _harness: None,
    )
    daemon = SimpleNamespace(
        config=config,
        controls=SimpleNamespace(
            route_for=lambda _participant_id, _capability: SimpleNamespace(route_available=False)
        ),
        operation_service=SimpleNamespace(
            wait=lambda _operation_id: _operation_wait("running", timed_out=True)
        ),
        registry=SimpleNamespace(get=lambda _participant_id: participant),
        store=SimpleNamespace(),
    )
    monkeypatch.setattr(spawning, "ParticipantLaunchService", lambda _daemon: LaunchService())
    monkeypatch.setattr(spawning, "check_depth", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(spawning, "check_budget", lambda *_args, **_kwargs: None)

    result = await spawning._spawn(
        daemon,
        {
            "harness": "vibe",
            "prompt": "work",
            "approval": "manual",
            "cwd": "/workspace",
        },
    )

    assert result["operation_id"] == "operation-a"
    assert result["operation_state"] == "running"
    assert result["operation_timed_out"] is True
    assert result["addressable"] is False


async def _operation_wait(state: str, *, timed_out: bool):
    return SimpleNamespace(state=state, error=None), timed_out
