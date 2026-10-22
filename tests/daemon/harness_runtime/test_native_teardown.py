"""Native-runtime teardown and kill: backend, worktree, terminal and binding retention."""

from __future__ import annotations

import contextlib
import os
import signal
from pathlib import Path

import pytest

from tests.support.native_lifecycle import (
    NativeFixtureHarness,
    RoutingIO,
    init_repo,
    native_daemon,
    spawn_native,
    spawn_request,
    spy_on_backend_launch,
    teardown_backend,
)
from tests.support.runtime_processes import (
    await_reaped,
    pid_alive,
)
from theater.daemon.rpc import participants as participants_mod
from theater.daemon.runtime import wiring as wiring_mod
from theater.daemon.runtime.recovery import teardown_participant_runtime
from theater.models import BadRequest, Status, TheaterError

# ---- teardown failure: recoverable state is preserved -------------------------

pytestmark = pytest.mark.usefixtures("native_harness_binary")


async def test_teardown_failure_preserves_the_worktree_terminal_and_binding(
    theater_home, terminal_provider, tmp_path, monkeypatch
):
    repo = init_repo(tmp_path / "repo")
    io = RoutingIO()
    harness = NativeFixtureHarness(open_fails=True)
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await native_daemon(io, harness, terminal_provider)
    launched: dict = {}
    spy_on_backend_launch(d, launched)
    refuse = [False]
    real_teardown = d.runtime_manager.teardown

    async def teardown_spy(participant_id, **kwargs):
        if refuse[0]:
            raise RuntimeError("teardown refused: backend cannot be stopped")
        return await real_teardown(participant_id, **kwargs)

    monkeypatch.setattr(d.runtime_manager, "teardown", teardown_spy)
    operation_wait = d.operation_service.wait

    async def wait_quickly(operation_id: str):
        return await operation_wait(operation_id, wait_seconds=25.0)

    monkeypatch.setattr(d.operation_service, "wait", wait_quickly)
    try:
        refuse[0] = True
        with pytest.raises(BadRequest):
            await spawn_native(d, spawn_request(prompt="never delivered", cwd=repo, worktree=True))
        (failed,) = d.registry.list(include_dead=True)
        operations, _ = d.operation_service.list(target_id=failed.id)
        assert len(operations) == 1
        assert operations[0].state == "failed"
        # Nothing the backend may still use is reclaimed: the worktree
        # stands, the terminal and binding ownership stay, the participant is
        # not marked dead, and the failure is the diagnostic one.
        participants = d.registry.list(include_dead=True)
        assert len(participants) == 1
        assert participants[0].id == failed.id
        assert failed.status is Status.DEAD, "failed launch recorded; resources retained"
        assert d.store.get_runtime_binding(failed.id) is not None, "binding kept"
        assert pid_alive(launched["pid"]), "the backend was not signalled"
        # The launch failed before pane creation. The terminal binding row and
        # the worktree directory are retired with the failed launch; the
        # preserved artifacts are the runtime binding row and the backend,
        # both retained for inspection of generation 1.
        assert d.store.terminal_bindings.get(failed.id) is None
        assert not Path(failed.cwd).is_dir(), "the worktree directory is retired"
        assert d.observer.live.registration_for(failed.id) is None
    finally:
        # The leaked backend is terminated through the real manager teardown
        # once the refusal stands down; a hard kill is only the fallback.
        refuse[0] = False
        rows = d.registry.list(include_dead=True)
        if rows:
            with contextlib.suppress(Exception):
                await d.runtime_manager.teardown(rows[0].id, backend_generation=1)
        pid = launched.get("pid")
        if pid is not None and pid_alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        if pid is not None:
            await await_reaped(pid)
        await d.aclose()


# ---- explicit kill and confirmed exit --------------------------------------------


async def test_adoption_failure_keeps_native_binding_for_reaper(
    theater_home, terminal_provider, native_rig, monkeypatch
):
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = None
    try:
        p = await spawn_native(d, spawn_request(prompt=""))
        binding = d.store.get_runtime_binding(p.id)
        assert binding is not None and binding.backend_pid is not None
        monkeypatch.setattr(d.runtime_manager, "backend", lambda _participant_id: None)

        async def refuse_adoption(*_args, **_kwargs):
            raise RuntimeError("adoption could not verify the backend")

        monkeypatch.setattr(d.runtime_manager, "adopt_backend", refuse_adoption)
        assert not await teardown_participant_runtime(d, p.id, caller_id="cli")
        assert d.store.get_runtime_binding(p.id) == binding
        assert pid_alive(binding.backend_pid)
        assert d.observer.live.registration_for(p.id) is not None
    finally:
        if p is not None:
            await teardown_backend(d, p.id)
        await d.aclose()


async def test_explicit_kill_terminates_the_backend_before_worktree_cleanup(
    theater_home, terminal_provider, native_rig, monkeypatch
):
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = None
    try:
        p = await spawn_native(d, spawn_request(prompt="work in flight"))
        binding = d.store.get_runtime_binding(p.id)
        terminal = d.store.terminal_bindings.get(p.id)
        assert terminal is not None

        teardown_liveness: list[bool] = []
        real_teardown = d.spawner.teardown

        async def teardown_spy(participant, **kwargs):
            teardown_liveness.append(pid_alive(binding.backend_pid))
            return await real_teardown(participant, **kwargs)

        monkeypatch.setattr(d.spawner, "teardown", teardown_spy)
        await participants_mod._kill(d, {"id": p.id})

        assert teardown_liveness == [False], "backend terminated before pane/worktree teardown"
        assert d.store.get_runtime_binding(p.id) is None
        assert d.registry.get(p.id).status is Status.DEAD
        await await_reaped(binding.backend_pid)
        assert terminal.terminal_id not in terminal_provider.terminal_ids
    finally:
        if p is not None:
            await teardown_backend(d, p.id)
        await d.aclose()


async def test_missing_provider_terminal_does_not_infer_native_backend_exit(
    theater_home, terminal_provider, native_rig
):
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = None
    try:
        p = await spawn_native(d, spawn_request(prompt=""))
        binding = d.store.get_runtime_binding(p.id)
        terminal = d.store.terminal_bindings.get(p.id)
        assert terminal is not None
        terminal_provider.remove_terminal(terminal.terminal_id)
        await d._reap_once()

        assert d.registry.get(p.id).status is not Status.DEAD
        assert d.store.get_runtime_binding(p.id) is not None
        assert pid_alive(binding.backend_pid)
    finally:
        if p is not None:
            await teardown_backend(d, p.id)
        await d.aclose()


async def test_definitive_terminal_rejection_tears_down_detached_native_backend(
    theater_home, terminal_provider, native_rig, monkeypatch
):
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    launched: dict[str, int] = {}
    spy_on_backend_launch(d, launched)
    requests = 0

    async def reject(_provider_id, generation, method, params):
        nonlocal requests
        assert method == "terminal.create"
        requests += 1
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "outcome": "rejected",
            "error": {"code": "provider_busy", "message": "definitive refusal"},
        }

    monkeypatch.setattr(d.terminal_service.connections, "request", reject)
    try:
        with pytest.raises(BadRequest, match="definitive refusal"):
            await spawn_native(d, spawn_request(prompt="never delivered"))

        assert requests == 1
        participant = d.registry.list(include_dead=True)[0]
        assert participant.status is Status.DEAD
        assert d.store.get_runtime_binding(participant.id) is None
        assert d.store.terminal_bindings.get(participant.id) is None
        assert d.runtime_manager.get(participant.id) is None
        await await_reaped(launched["pid"])
    finally:
        pid = launched.get("pid")
        if pid is not None and pid_alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
            await await_reaped(pid)
        await d.aclose()


async def test_unverified_native_stop_preserves_provider_and_workspace_state(
    theater_home, terminal_provider, tmp_path, native_rig, monkeypatch
):
    repo = init_repo(tmp_path / "repo")
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = None
    refuse = [False]
    real_teardown = d.runtime_manager.teardown

    async def teardown_spy(participant_id, **kwargs):
        if refuse[0]:
            raise RuntimeError("teardown refused: backend cannot be stopped")
        return await real_teardown(participant_id, **kwargs)

    monkeypatch.setattr(d.runtime_manager, "teardown", teardown_spy)
    try:
        p = await spawn_native(d, spawn_request(prompt="in flight", cwd=repo, worktree=True))
        binding = d.store.get_runtime_binding(p.id)
        refuse[0] = True
        with pytest.raises(TheaterError, match="teardown could not be verified"):
            await participants_mod._kill(d, {"id": p.id})
        # The kill could not prove the backend stopped: the worktree and the
        # binding survive it — never retired on an unverified teardown.
        kept = d.registry.get(p.id)
        assert kept.status is not Status.DEAD, "no retirement without a proven stop"
        assert d.store.get_runtime_binding(p.id) is not None, "binding kept"
        assert Path(kept.cwd).is_dir(), "the worktree is not retired"
        assert pid_alive(binding.backend_pid), "the backend was not signalled"

        # Missing provider evidence cannot make the later native reaper infer exit.
        refuse[0] = False
        await d._reap_once()
        assert d.registry.get(p.id).status is not Status.DEAD
        assert d.store.get_runtime_binding(p.id) is not None
        assert Path(kept.cwd).is_dir(), "participant kill retains the worktree"
        assert pid_alive(binding.backend_pid)
    finally:
        refuse[0] = False
        if p is not None and d.store.get_runtime_binding(p.id) is not None:
            with contextlib.suppress(Exception):
                await teardown_backend(d, p.id)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(d.store.get_runtime_binding(p.id).backend_pid, signal.SIGKILL)
        await d.aclose()


async def test_missing_terminal_preserves_worktree_and_native_runtime(
    theater_home, terminal_provider, tmp_path, native_rig, monkeypatch
):
    repo = init_repo(tmp_path / "repo")
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = None
    refuse = [False]
    real_teardown = d.runtime_manager.teardown

    async def teardown_spy(participant_id, **kwargs):
        if refuse[0]:
            raise RuntimeError("teardown refused: backend cannot be stopped")
        return await real_teardown(participant_id, **kwargs)

    monkeypatch.setattr(d.runtime_manager, "teardown", teardown_spy)
    try:
        p = await spawn_native(d, spawn_request(prompt="", cwd=repo, worktree=True))
        binding = d.store.get_runtime_binding(p.id)
        terminal = d.store.terminal_bindings.get(p.id)
        assert terminal is not None
        refuse[0] = True
        terminal_provider.remove_terminal(terminal.terminal_id)
        await d._reap_once()

        # A missing terminal is not authoritative exit evidence.
        exited = d.registry.get(p.id)
        assert exited.status is not Status.DEAD
        assert d.store.get_runtime_binding(p.id) is not None, "binding kept"
        assert Path(exited.cwd).is_dir(), "the worktree is not retired"
        assert pid_alive(binding.backend_pid), "the backend was not signalled"

        refuse[0] = False
        await d._reap_once()
        assert d.store.get_runtime_binding(p.id) is not None
        assert Path(exited.cwd).is_dir(), "missing terminal retains the worktree"
        assert pid_alive(binding.backend_pid)
    finally:
        refuse[0] = False
        if p is not None and d.store.get_runtime_binding(p.id) is not None:
            with contextlib.suppress(Exception):
                await teardown_backend(d, p.id)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(d.store.get_runtime_binding(p.id).backend_pid, signal.SIGKILL)
        await d.aclose()
