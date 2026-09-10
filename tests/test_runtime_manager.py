"""Focused tests for the daemon runtime manager.

Concurrency assertions use deterministic barriers (events the test releases
only after every caller has arrived), never timing. Close-without-kill,
generation guards, and concurrent get-or-create are exercised against both the
Wave 1 fake runtime and a real detached backend process.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.daemon.harness_runtime.backend import DetachedBackendProcess
from theater.daemon.harness_runtime.errors import (
    BackendAlreadyLaunched,
    RuntimeGenerationMismatch,
)
from theater.daemon.harness_runtime.manager import HarnessRuntimeManager
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import RuntimeContext, RuntimePlan

SLEEP_SNIPPET = "import time; time.sleep(300)"


def _runtime_context(participant_id: str, state: FakeRuntimeState) -> RuntimeContext:
    return RuntimeContext(
        participant_id=participant_id,
        cwd=None,
        io=FakeRuntimeIO(state),
        backend_generation=state.backend_generation,
        endpoint=f"unix:///tmp/thtr-{participant_id}.sock",
    )


def _factory(participant_id: str, state: FakeRuntimeState):
    async def create() -> FakeRuntime:
        return FakeRuntime(_runtime_context(participant_id, state))

    return create


async def test_get_never_creates_anything() -> None:
    manager = HarnessRuntimeManager()
    assert manager.get("p1") is None
    assert manager.backend("p1") is None
    assert manager.participants() == ()


async def test_concurrent_get_or_create_builds_exactly_one_instance() -> None:
    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    barrier = asyncio.Event()
    creations = 0

    async def create() -> FakeRuntime:
        nonlocal creations
        creations += 1
        # Deterministic barrier: hold creation open until the test releases
        # it, so every concurrent caller must queue behind one creation.
        await barrier.wait()
        return FakeRuntime(_runtime_context("p1", state))

    callers = 8
    tasks = [
        asyncio.create_task(manager.get_or_create("p1", backend_generation=1, create=create))
        for _ in range(callers)
    ]
    while creations < 1:
        await asyncio.sleep(0)  # the first caller is now parked inside create()
    for _ in range(callers):
        await asyncio.sleep(0)  # everyone else is parked on the participant lock
    barrier.set()
    results = await asyncio.gather(*tasks)
    assert creations == 1, "concurrent callers must share one instance"
    assert all(instance is results[0] for instance in results)


async def test_generation_change_disconnects_old_runtime_and_binds_new() -> None:
    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    first = await manager.get_or_create("p1", backend_generation=1, create=_factory("p1", state))
    assert first.state.connected is True
    second = await manager.get_or_create("p1", backend_generation=2, create=_factory("p1", state))
    assert second is not first
    assert first.state.connected is False, "the old generation's runtime is disconnected"
    assert manager.get("p1") is second


async def test_reconnect_closes_and_recreates_for_the_same_generation() -> None:
    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    first = await manager.get_or_create("p1", backend_generation=7, create=_factory("p1", state))
    second = await manager.reconnect("p1", backend_generation=7, create=_factory("p1", state))
    assert second is not first
    assert first.state.connected is False
    assert manager.get("p1") is second


async def test_close_disconnects_but_terminates_no_backend() -> None:
    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    runtime = await manager.get_or_create("p1", backend_generation=1, create=_factory("p1", state))
    await manager.close("p1")
    assert runtime.state.connected is False
    assert manager.get("p1") is None
    await manager.aclose()  # no backend was ever owned; both are safe no-ops


async def test_teardown_with_wrong_generation_fails_closed(tmp_path: Path) -> None:
    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    runtime = await manager.get_or_create("p1", backend_generation=1, create=_factory("p1", state))
    plan = RuntimePlan(
        backend=LaunchPlan(argv=[sys.executable, "-c", SLEEP_SNIPPET]),
        endpoint="unix:///tmp/thtr-mgr-mismatch.sock",
    )
    await manager.launch_backend("p1", backend_generation=1, plan=plan, cwd=tmp_path)
    backend = manager.backend("p1")
    assert backend is not None and backend.alive()
    with pytest.raises(RuntimeGenerationMismatch, match="refuses"):
        await manager.teardown("p1", backend_generation=2)
    assert backend.alive(), "a stale generation must never reach the backend"
    assert runtime.state.connected is True, "and never disconnect the live runtime either"
    await manager.teardown("p1", backend_generation=1)
    assert not backend.alive()
    assert runtime.state.connected is False
    assert manager.get("p1") is None
    assert manager.backend("p1") is None


async def test_close_without_kill_leaves_the_backend_alive(tmp_path: Path) -> None:
    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    await manager.get_or_create("p1", backend_generation=1, create=_factory("p1", state))
    plan = RuntimePlan(
        backend=LaunchPlan(argv=[sys.executable, "-c", SLEEP_SNIPPET]),
        endpoint="unix:///tmp/thtr-mgr-close.sock",
    )
    await manager.launch_backend("p1", backend_generation=1, plan=plan, cwd=tmp_path)
    backend = manager.backend("p1")
    assert isinstance(backend, DetachedBackendProcess)
    # Close the runtime connection explicitly: the backend must survive.
    await manager.close("p1")
    assert manager.get("p1") is None
    assert backend.alive(), "closing a runtime never terminates its backend"
    # Teardown is the only path that terminates: verify identity then kill.
    await manager.teardown("p1", backend_generation=1)
    assert not backend.alive()


async def test_launch_backend_refuses_to_orphan_a_live_generation(tmp_path: Path) -> None:
    manager = HarnessRuntimeManager()
    plan = RuntimePlan(
        backend=LaunchPlan(argv=[sys.executable, "-c", SLEEP_SNIPPET]),
        endpoint="unix:///tmp/thtr-mgr-orphan.sock",
    )
    await manager.launch_backend("p1", backend_generation=1, plan=plan, cwd=tmp_path)
    with pytest.raises(BackendAlreadyLaunched, match="never launch a second"):
        await manager.launch_backend("p1", backend_generation=1, plan=plan, cwd=tmp_path)
    other = RuntimePlan(
        backend=LaunchPlan(argv=[sys.executable, "-c", SLEEP_SNIPPET]),
        endpoint="unix:///tmp/thtr-mgr-orphan2.sock",
    )
    with pytest.raises(BackendAlreadyLaunched, match="teardown that generation"):
        await manager.launch_backend("p1", backend_generation=2, plan=other, cwd=tmp_path)
    await manager.teardown("p1", backend_generation=1)
    # After teardown, a new generation may launch cleanly.
    identity = await manager.launch_backend("p1", backend_generation=2, plan=other, cwd=tmp_path)
    assert identity.pid > 0
    await manager.teardown("p1", backend_generation=2)


async def test_history_reads_have_no_creation_path(tmp_path: Path) -> None:
    """A short-lived read path can never create a runtime, backend, or connection."""
    manager = HarnessRuntimeManager()
    created: list[str] = []

    async def create() -> FakeRuntime:
        created.append("created")
        raise AssertionError("the history path must never create a runtime")

    assert manager.get("reader") is None
    assert manager.backend("reader") is None
    assert created == []


async def test_teardown_of_unknown_participant_is_a_no_op() -> None:
    manager = HarnessRuntimeManager()
    await manager.teardown("nobody", backend_generation=1)
    assert manager.participants() == ()


async def test_aclose_disconnects_all_runtimes_and_terminates_nothing(
    tmp_path: Path,
) -> None:
    manager = HarnessRuntimeManager()
    state_one = FakeRuntimeState(participant_id="pa")
    state_two = FakeRuntimeState(participant_id="pb")
    runtime_one = await manager.get_or_create(
        "pa", backend_generation=1, create=_factory("pa", state_one)
    )
    runtime_two = await manager.get_or_create(
        "pb", backend_generation=1, create=_factory("pb", state_two)
    )
    plan = RuntimePlan(
        backend=LaunchPlan(argv=[sys.executable, "-c", SLEEP_SNIPPET]),
        endpoint="unix:///tmp/thtr-mgr-all.sock",
    )
    await manager.launch_backend("pa", backend_generation=1, plan=plan, cwd=tmp_path)
    backend = manager.backend("pa")
    await manager.aclose()
    assert runtime_one.state.connected is False
    assert runtime_two.state.connected is False
    assert backend is not None and backend.alive(), "shutdown never kills backends"
    await manager.teardown("pa", backend_generation=1)
    assert backend is not None and not backend.alive()
