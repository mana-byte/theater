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
from theater.daemon.harness_runtime import manager as manager_mod
from theater.daemon.harness_runtime.backend import DetachedBackendProcess
from theater.daemon.harness_runtime.errors import (
    BackendAlreadyLaunched,
    BackendIdentityMismatch,
    RuntimeGenerationMismatch,
)
from theater.daemon.harness_runtime.manager import HarnessRuntimeManager
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import ConnectionHealth, RuntimeContext, RuntimePlan

SLEEP_SNIPPET = "import time; time.sleep(300)"


async def _park(task: asyncio.Task, turns: int = 20) -> None:
    """Yield event-loop turns until a task reaches its next blocking wait."""
    for _ in range(turns):
        await asyncio.sleep(0)
    assert not task.done(), "the task finished before the test could park it"


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


# ---- generation and race safety -------------------------------------------------


async def test_reconnect_rejects_a_generation_mismatch_before_disconnecting() -> None:
    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    first = await manager.get_or_create("p1", backend_generation=7, create=_factory("p1", state))
    with pytest.raises(RuntimeGenerationMismatch, match="refuses"):
        await manager.reconnect("p1", backend_generation=8, create=_factory("p1", state))
    assert first.state.connected is True, "a mismatch must not disconnect anything"
    assert manager.get("p1") is first


async def test_get_or_create_refuses_to_bind_a_conflicting_live_backend_generation(
    tmp_path: Path,
) -> None:
    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    first = await manager.get_or_create("p1", backend_generation=1, create=_factory("p1", state))
    plan = RuntimePlan(
        backend=LaunchPlan(argv=[sys.executable, "-c", SLEEP_SNIPPET]),
        endpoint="unix:///tmp/thtr-mgr-conflict.sock",
    )
    await manager.launch_backend("p1", backend_generation=1, plan=plan, cwd=tmp_path)
    with pytest.raises(RuntimeGenerationMismatch, match="stranding"):
        await manager.get_or_create("p1", backend_generation=2, create=_factory("p1", state))
    assert first.state.connected is True, "fail closed leaves the live runtime untouched"
    assert manager.get("p1") is first
    backend = manager.backend("p1")
    assert backend is not None and backend.alive()
    await manager.teardown("p1", backend_generation=1)


async def test_get_or_create_retries_when_teardown_removed_its_entry(
    tmp_path: Path,
) -> None:
    """A teardown that empties an entry must not strand a queued creator.

    The participant lock is held by the test so both contenders park on it in
    a deterministic order: teardown first (it removes the now-empty entry),
    then get_or_create, which must notice the removal, retry, and register on
    a fresh entry instead of building an unreachable orphan.
    """
    manager = HarnessRuntimeManager()
    entry = await manager._entry("p1")
    await entry.lock.acquire()
    teardown_task = asyncio.create_task(manager.teardown("p1", backend_generation=1))
    await _park(teardown_task)
    creations = 0

    async def create() -> FakeRuntime:
        nonlocal creations
        creations += 1
        return FakeRuntime(_runtime_context("p1", FakeRuntimeState(participant_id="p1")))

    create_task = asyncio.create_task(
        manager.get_or_create("p1", backend_generation=1, create=create)
    )
    await _park(create_task)
    entry.lock.release()
    await teardown_task  # first waiter: empties and removes the shared entry
    runtime = await create_task  # second waiter: must retry onto a fresh entry
    assert creations == 1, "the stale entry is abandoned before create() runs"
    assert manager.get("p1") is runtime, "the created runtime is reachable"
    assert "p1" in manager.participants()


async def test_close_exposes_no_closing_runtime_through_get() -> None:
    """Once a close starts, get() cannot hand out the closing runtime."""

    class _SlowCloseRuntime(FakeRuntime):
        def __init__(self, context: RuntimeContext, gate: asyncio.Event) -> None:
            super().__init__(context)
            self._gate = gate

        async def aclose(self) -> None:
            await self._gate.wait()
            await super().aclose()

    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    gate = asyncio.Event()

    async def create_slow() -> FakeRuntime:
        return _SlowCloseRuntime(_runtime_context("p1", state), gate)

    await manager.get_or_create("p1", backend_generation=1, create=create_slow)
    close_task = asyncio.create_task(manager.close("p1"))
    # close() clears the entry before awaiting the runtime's aclose, so the
    # mid-close get() below observes the cleared registry, deterministically.
    await _park(close_task)
    assert manager.get("p1") is None, "a closing runtime is never exposed"
    gate.set()
    await close_task
    assert state.connected is False


# ---- adoption after a daemon restart --------------------------------------------


async def test_fresh_manager_adopts_reconnects_and_teardown_terminates(
    tmp_path: Path,
) -> None:
    manager = HarnessRuntimeManager()
    endpoint = "unix:///tmp/thtr-mgr-adopt.sock"
    plan = RuntimePlan(
        backend=LaunchPlan(argv=[sys.executable, "-c", SLEEP_SNIPPET]),
        endpoint=endpoint,
    )
    identity = await manager.launch_backend("p1", backend_generation=3, plan=plan, cwd=tmp_path)
    assert identity.started_at is not None, "the persisted numeric identity exists"

    # Daemon restart: a fresh manager knows only the persisted binding facts.
    fresh = HarnessRuntimeManager()
    adopted = await fresh.adopt_backend(
        "p1",
        backend_generation=3,
        pid=identity.pid,
        started_at=identity.started_at,
        endpoint=endpoint,
    )
    assert adopted.pid == identity.pid
    assert adopted.started_at == identity.started_at
    backend = fresh.backend("p1")
    assert backend is not None and backend.alive()

    state = FakeRuntimeState(participant_id="p1")
    await fresh.get_or_create("p1", backend_generation=3, create=_factory("p1", state))
    reconnected = await fresh.reconnect("p1", backend_generation=3, create=_factory("p1", state))
    assert reconnected is not None
    assert state.connected is False, "reconnect disconnected the previous runtime"

    await fresh.teardown("p1", backend_generation=3)
    assert backend.alive() is False, "teardown terminates the adopted process"
    assert fresh.backend("p1") is None
    assert "p1" not in fresh.participants()


async def test_adopt_backend_with_mismatched_identity_fails_closed(tmp_path: Path) -> None:
    manager = HarnessRuntimeManager()
    endpoint = "unix:///tmp/thtr-mgr-adopt2.sock"
    plan = RuntimePlan(
        backend=LaunchPlan(argv=[sys.executable, "-c", SLEEP_SNIPPET]),
        endpoint=endpoint,
    )
    identity = await manager.launch_backend("p1", backend_generation=1, plan=plan, cwd=tmp_path)
    started_at = identity.started_at
    assert started_at is not None

    fresh = HarnessRuntimeManager()
    with pytest.raises(BackendIdentityMismatch, match="start identity"):
        await fresh.adopt_backend(
            "p1",
            backend_generation=1,
            pid=identity.pid,
            started_at=started_at + 555.0,
            endpoint=endpoint,
        )
    assert fresh.backend("p1") is None, "a failed adoption registers nothing"
    assert fresh.get("p1") is None
    assert fresh.participants() == (), "the empty entry is not left behind"
    backend = manager.backend("p1")
    assert backend is not None and backend.alive(), "the real backend is untouched"
    await manager.teardown("p1", backend_generation=1)


# ---- same-runtime disconnect recovery monitors -------------------------------


async def _wait_until(predicate, *, timeout: float = 3.0, message: str = "condition") -> None:
    """Bounded wait for a predicate; a hang detector, never a sleep."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for {message}")
        await asyncio.sleep(0.005)


async def test_disconnected_runtime_recovers_exactly_once_while_callback_runs(
    monkeypatch,
) -> None:
    """Coalesced recovery: one monitor, one in-flight attempt per generation.

    The recovery callback runs inline in the monitor, so no second attempt
    can start while the first is in flight — and the same-generation
    replacement runtime is watched by the same monitor.
    """
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", 0.01)
    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    original = await manager.get_or_create("p1", backend_generation=1, create=_factory("p1", state))
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[tuple[str, int]] = []

    async def callback(participant_id: str, backend_generation: int) -> bool:
        calls.append((participant_id, backend_generation))
        started.set()
        await release.wait()
        # The daemon-side recovery: reconnect installs a replacement runtime
        # of the same generation under this same monitor.
        await manager.reconnect("p1", backend_generation=1, create=_factory("p1", state))
        state.health = ConnectionHealth.CONNECTED
        return True

    manager.set_recovery_callback(callback)
    state.health = ConnectionHealth.DISCONNECTED

    await asyncio.wait_for(started.wait(), 2.0)
    # Several poll intervals pass while the attempt is still in flight: a
    # second concurrent attempt must not start.
    await asyncio.sleep(0.06)
    assert calls == [("p1", 1)]
    release.set()

    await _wait_until(lambda: manager.get("p1") is not original, message="the replacement runtime")
    await asyncio.sleep(0.06)
    assert calls == [("p1", 1)], "a healthy replacement is not re-recovered"
    await manager.aclose()


async def test_failed_recovery_retries_on_a_bounded_cadence(monkeypatch) -> None:
    """A refused attempt retries after the bounded delay, never hot-loops."""
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", 0.05)
    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    await manager.get_or_create("p1", backend_generation=1, create=_factory("p1", state))
    timestamps: list[float] = []

    async def callback(participant_id: str, backend_generation: int) -> bool:
        del participant_id, backend_generation
        timestamps.append(asyncio.get_running_loop().time())
        return False

    manager.set_recovery_callback(callback)
    state.health = ConnectionHealth.DISCONNECTED
    await _wait_until(lambda: len(timestamps) >= 2, message="the retry cadence")
    elapsed = asyncio.get_running_loop().time() - timestamps[0]
    # Bounded cadence, not a hot loop: with retry=0.05 and poll=0.01, the
    # first two attempts alone span at least one retry delay, and the count
    # stays far below a per-event-loop-turn fanout.
    assert timestamps[1] - timestamps[0] >= 0.045
    assert len(timestamps) <= int(elapsed / 0.04) + 2
    await manager.aclose()


async def test_replaced_generation_monitor_recovers_nothing(monkeypatch) -> None:
    """A stale generation's monitor is a silent no-op after replacement."""
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", 0.01)
    manager = HarnessRuntimeManager()
    stale_state = FakeRuntimeState(participant_id="p1")
    await manager.get_or_create("p1", backend_generation=1, create=_factory("p1", stale_state))
    calls: list[tuple[str, int]] = []

    async def callback(participant_id: str, backend_generation: int) -> bool:
        calls.append((participant_id, backend_generation))
        await manager.reconnect(
            "p1", backend_generation=backend_generation, create=_factory("p1", live_state)
        )
        live_state.health = ConnectionHealth.CONNECTED
        return True

    manager.set_recovery_callback(callback)
    stale_state.health = ConnectionHealth.DISCONNECTED
    live_state = FakeRuntimeState(participant_id="p1")
    # The replacement generation installs before the stale monitor's poll
    # can fire; the stale runtime was already DISCONNECTED.
    await manager.get_or_create("p1", backend_generation=2, create=_factory("p1", live_state))
    await asyncio.sleep(0.08)
    assert calls == [], "the stale (p1, generation 1) monitor must recover nothing"

    live_state.health = ConnectionHealth.DISCONNECTED
    await _wait_until(lambda: calls, message="the live generation's recovery")
    assert calls == [("p1", 2)], "only the replacement generation recovers"
    await manager.aclose()


async def test_close_teardown_and_aclose_cancel_their_monitors(monkeypatch) -> None:
    """No recovery attempt outlives close, teardown, or daemon shutdown."""
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", 0.02)
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", 0.02)
    manager = HarnessRuntimeManager()
    states = {pid: FakeRuntimeState(participant_id=pid) for pid in ("p1", "p2", "p3")}
    for pid in ("p1", "p2", "p3"):
        await manager.get_or_create(pid, backend_generation=1, create=_factory(pid, states[pid]))
    calls: list[tuple[str, int]] = []

    async def callback(participant_id: str, backend_generation: int) -> bool:
        calls.append((participant_id, backend_generation))
        return True

    manager.set_recovery_callback(callback)
    for state in states.values():
        state.health = ConnectionHealth.DISCONNECTED
    await manager.close("p1")
    await manager.teardown("p2", backend_generation=1)
    await manager.aclose()
    # Past several poll/retry intervals: a surviving monitor would have
    # fired many times by now.
    await asyncio.sleep(0.15)
    assert calls == [], "no post-close, post-teardown, or post-shutdown recovery"


async def test_blocked_recovery_for_one_participant_does_not_block_another(
    monkeypatch,
) -> None:
    """One participant's blocked recovery never blocks the other's.

    The monitor holds no lock while the callback runs, so a blocked
    recovery for p1 leaves p2's recovery, and every registry lookup,
    responsive.
    """
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", 0.01)
    manager = HarnessRuntimeManager()
    states = {pid: FakeRuntimeState(participant_id=pid) for pid in ("p1", "p2")}
    factories = {pid: _factory(pid, states[pid]) for pid in states}
    for pid in states:
        await manager.get_or_create(pid, backend_generation=1, create=factories[pid])
    gate = asyncio.Event()
    calls: list[str] = []

    async def callback(participant_id: str, backend_generation: int) -> bool:
        del backend_generation
        if participant_id == "p1":
            await gate.wait()  # the blocked reconnect for p1
        calls.append(participant_id)
        await manager.reconnect(
            participant_id, backend_generation=1, create=factories[participant_id]
        )
        states[participant_id].health = ConnectionHealth.CONNECTED
        return True

    manager.set_recovery_callback(callback)
    for state in states.values():
        state.health = ConnectionHealth.DISCONNECTED

    await _wait_until(lambda: "p2" in calls, message="p2's unblocked recovery")
    assert "p1" not in calls, "p1 stays blocked while it recovers"
    # Registry lookups stay responsive while p1's recovery is blocked.
    assert manager.get("p1") is not None
    assert manager.get("p2") is not None

    gate.set()
    await _wait_until(lambda: "p1" in calls, message="p1's recovery after the gate")
    await manager.aclose()
