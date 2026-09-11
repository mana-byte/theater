"""Focus-protection regressions across the daemon's control surfaces."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from tests._presence_doubles import (
    AbsentPresence,
    PresentPresence,
    UnknownPresence,
    _BasePresence,
)
from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.constants.daemon import (
    BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED,
    BUS_KIND_PARTICIPANT_KILL_REQUESTED,
    BUS_KIND_SEND_REFUSED,
)
from theater.daemon.controls import ControlGates, ControlService
from theater.daemon.jobs import JobManager, JobState
from theater.daemon.persistence.repositories.runtime_bindings import (
    ParticipantRuntimeBinding,
)
from theater.daemon.presence import PresenceState
from theater.daemon.schema import control_operations as control_operations_table
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    RuntimeCapability,
    RuntimeContext,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import Busy, HumanPresent, Status
from theater.protocol import RemoteError

_OWNED_SERVICES: list[ControlService] = []


@pytest.fixture(autouse=True)
def _restore_composed_presence(request):
    """Restore the owned monitor before a daemon fixture shuts down."""
    if "daemon" not in request.fixturenames:
        yield
        return
    daemon = request.getfixturevalue("daemon")
    original = getattr(daemon, "presence", None)
    try:
        yield
    finally:
        daemon.presence = original


@pytest.fixture(autouse=True)
async def _close_owned_services():
    """Every ControlService this module builds is closed at test end."""
    yield
    while _OWNED_SERVICES:
        await _OWNED_SERVICES.pop().aclose()


# ---- service-level rig ------------------------------------------------------


class PresenceGates:
    """A ControlGates presence seam with per-target scripted refusals."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        #: Targets whose presence gate always refuses.
        self.refusals: set[str] = set()
        #: Targets refused only after N passing calls (focus arrives mid-flight).
        self.passes_before_refusal: dict[str, int] = {}
        #: Real provider double; when set, it decides every check.
        self.provider: _BasePresence | None = None
        #: Optional exception send_preflight raises after signaling preflight_parked.
        self.preflight_exception: Exception | None = None
        self.preflight_parked = asyncio.Event()

    async def require_absent(self, participant_id: str) -> None:
        if self.provider is not None:
            await self.provider.require_absent(participant_id)
            return
        calls = self.calls.get(participant_id, 0) + 1
        self.calls[participant_id] = calls
        passes = self.passes_before_refusal.get(participant_id)
        if participant_id in self.refusals or (passes is not None and calls > passes):
            raise HumanPresent(f"human focus protects {participant_id!r}")

    def gates(self) -> ControlGates:
        async def noop(*args, **kwargs) -> None:
            return None

        return ControlGates(
            authorize=lambda *args: None,
            require_absent=self.require_absent,
            send_preflight=self._send_preflight,
            legacy_copy_mode_check=noop,
            legacy_busy_check=noop,
            check_prompt=lambda prompt: None,
            check_settings=lambda model, effort: None,
            cwd_for=lambda participant_id: None,
            legacy_deliver=noop,
        )

    async def _send_preflight(self, participant_id: str) -> None:
        if self.preflight_exception is not None:
            self.preflight_parked.set()
            await asyncio.sleep(0)
            raise self.preflight_exception


class Rig:
    """One control service over one store with per-participant fake runtimes."""

    def __init__(self, store, *pids: str):
        self.store = store
        self.jobs = JobManager(store)
        self.presence = PresenceGates()
        self.runtimes = {}
        for pid in pids:
            state = FakeRuntimeState(participant_id=pid, backend_generation=1)
            state.native_session_id = f"thread-{pid}"
            state.backend_alive = True
            context = RuntimeContext(
                participant_id=pid,
                cwd=None,
                io=FakeRuntimeIO(state),
                backend_generation=1,
            )
            self.runtimes[pid] = FakeRuntime(context)
        self.service = ControlService(
            store=store,
            jobs=self.jobs,
            runtime_for=self.runtimes.get,
            gates=self.presence.gates(),
        )
        _OWNED_SERVICES.append(self.service)

    def state(self, pid: str) -> FakeRuntimeState:
        return self.runtimes[pid].state


def _operation_rows(store, pid: str) -> list[dict]:
    return [
        dict(row._mapping)
        for row in store.conn.execute(
            select(control_operations_table).where(control_operations_table.c.participant_id == pid)
        ).fetchall()
    ]


def _queue_payload(store, pid: str) -> str | None:
    """The payload of the first queued followup operation, if any."""
    for row in _operation_rows(store, pid):
        if row["kind"] == "queue_followup" and row["delivery_phase"] == "queued":
            return row["payload"]
    return None


async def _queue_idle(rig: Rig, pid: str, *prompts: str) -> list[str]:
    """Queue followups that stay pending while the turn is busy, then idle."""
    rig.state(pid).native_turn_id = "busy-turn"
    jobs = []
    try:
        for prompt in prompts:
            jobs.append(
                (await rig.service.queue_followup(pid, caller_id="caller", prompt=prompt)).handle
            )
    finally:
        rig.state(pid).native_turn_id = None
    return jobs


# ---- service-level gates ----------------------------------------------------


async def test_send_refused_for_present_focus_mints_nothing(store):
    """A real PRESENT provider state refuses before anything is minted."""
    rig = Rig(store, "p1")
    rig.presence.provider = PresentPresence()

    with pytest.raises(HumanPresent):
        await rig.service.send("p1", caller_id="caller", prompt="hi")

    assert _operation_rows(store, "p1") == []
    assert store.active_running_jobs_for_target("p1") == []
    assert rig.state("p1").sent == []


async def test_unknown_focus_defers_queue_dispatch_and_keeps_fifo(store):
    """A real UNKNOWN provider state defers dispatch; FIFO resumes after."""
    rig = Rig(store, "p1")
    (handle,) = await _queue_idle(rig, "p1", "later")
    rig.presence.provider = UnknownPresence()

    assert (await rig.service.dispatch_queue("p1")).deferred is True
    assert store.queued_control_operation_count("p1") == 1
    assert store.get_job(handle).state == "running"
    assert rig.state("p1").sent == []

    rig.presence.provider.set_state(PresenceState.ABSENT, "facts returned")
    outcome = await rig.service.dispatch_queue("p1")
    assert outcome.dispatched == (handle,)
    assert rig.state("p1").sent == ["later"]


async def test_send_refused_when_focus_arrives_during_preparation(store):
    """The recheck after the awaited snapshot refuses before job creation."""
    rig = Rig(store, "p1")
    rig.presence.passes_before_refusal = {"p1": 1}

    with pytest.raises(HumanPresent):
        await rig.service.send("p1", caller_id="caller", prompt="hi")

    assert _operation_rows(store, "p1") == []
    assert store.active_running_jobs_for_target("p1") == []
    assert rig.state("p1").sent == []


async def test_steer_refused_for_present_focus_leaves_the_job_untouched(store):
    rig = Rig(store, "p1")
    job = await rig.service.send("p1", caller_id="caller", prompt="first")
    rig.presence.refusals = {"p1"}

    with pytest.raises(HumanPresent):
        await rig.service.steer("p1", caller_id="caller", prompt="amend")

    assert store.get_job(job.handle).state == "running"
    assert rig.state("p1").steered == []
    kinds = {row["kind"] for row in _operation_rows(store, "p1")}
    assert "steer" not in kinds


async def test_queue_admission_refused_for_present_focus(store):
    rig = Rig(store, "p1")
    rig.presence.refusals = {"p1"}

    with pytest.raises(HumanPresent):
        await rig.service.queue_followup("p1", caller_id="caller", prompt="later")

    assert _operation_rows(store, "p1") == []
    assert store.active_running_jobs_for_target("p1") == []


async def test_interrupt_preserves_the_queue_when_focus_is_present(store):
    rig = Rig(store, "p1")
    await rig.service.send("p1", caller_id="caller", prompt="first")
    (queued,) = await _queue_idle(rig, "p1", "later")
    rig.presence.refusals = {"p1"}

    with pytest.raises(HumanPresent):
        await rig.service.interrupt("p1", caller_id="caller")

    assert store.get_job(queued).state == "running"
    assert store.queued_control_operation_count("p1") == 1
    assert rig.state("p1").interrupted == []


async def test_settings_refused_for_present_focus(store):
    rig = Rig(store, "p1")
    rig.presence.refusals = {"p1"}

    with pytest.raises(HumanPresent):
        await rig.service.update_settings("p1", caller_id="caller", model="some-model")

    assert _operation_rows(store, "p1") == []


async def test_queue_defers_while_present_and_resumes_fifo(store):
    rig = Rig(store, "p1")
    handles = await _queue_idle(rig, "p1", "one", "two")

    rig.presence.refusals = {"p1"}
    assert (await rig.service.dispatch_queue("p1")).deferred is True
    assert store.queued_control_operation_count("p1") == 2
    assert [op.job_handle for op in store.queued_control_operations("p1")] == handles

    rig.presence.refusals.clear()
    first = await rig.service.dispatch_queue("p1")
    assert first.dispatched == (handles[0],)
    # The dispatched turn ended: its job is finished before the next dispatch.
    rig.state("p1").native_turn_id = None
    rig.jobs.finish(handles[0], state=JobState.DONE, result="one")
    second = await rig.service.dispatch_queue("p1")
    assert second.dispatched == (handles[1],)
    assert rig.state("p1").sent == ["one", "two"]


async def test_native_dispatch_refusal_leaves_the_head_unchanged(store):
    """A deferred native head keeps its queue slot, job, and FIFO position."""
    rig = Rig(store, "p1")
    (handle,) = await _queue_idle(rig, "p1", "later")
    head_phase_before = [
        row["delivery_phase"]
        for row in _operation_rows(store, "p1")
        if row["kind"] == "queue_followup"
    ]

    rig.presence.refusals = {"p1"}
    assert (await rig.service.dispatch_queue("p1")).deferred is True

    phases_after = [
        row["delivery_phase"]
        for row in _operation_rows(store, "p1")
        if row["kind"] == "queue_followup"
    ]
    assert phases_after == head_phase_before
    assert store.get_job(handle).state == "running"
    assert rig.state("p1").sent == []

    rig.presence.refusals.clear()
    outcome = await rig.service.dispatch_queue("p1")
    assert outcome.dispatched == (handle,)
    assert rig.state("p1").sent == ["later"]


async def test_busy_dispatch_never_rebinds_the_head_while_focus_present(store):
    """Focus arriving during the awaited snapshot defers with no turn binding."""
    rig = Rig(store, "p1")
    (handle,) = await _queue_idle(rig, "p1", "later")
    rig.state("p1").native_turn_id = "human-turn-2"
    await asyncio.sleep(0)  # drain the admission-scheduled dispatch pass
    assert store.queued_control_operation_count("p1") == 1
    payload_before = _queue_payload(store, "p1")
    rig.state("p1").native_turn_id = "human-turn-3"
    runtime = rig.runtimes["p1"]
    original = runtime.snapshot
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_snapshot():
        entered.set()
        await release.wait()
        return await original()

    runtime.snapshot = blocked_snapshot
    dispatch = asyncio.create_task(rig.service.dispatch_queue("p1"))
    await entered.wait()
    rig.presence.provider = PresentPresence()
    release.set()

    assert (await dispatch).deferred is True
    assert _queue_payload(store, "p1") == payload_before
    assert store.queued_control_operation_count("p1") == 1
    assert store.get_job(handle).state == "running"
    assert rig.state("p1").sent == []

    # Departure restores the safe bookkeeping: the head rebinds behind the turn.
    rig.presence.provider = AbsentPresence()
    assert (await rig.service.dispatch_queue("p1")).deferred is True
    assert _queue_payload(store, "p1") == '{"queue_predecessor_turn": "human-turn-3"}'


async def test_native_interrupt_mid_snapshot_preserves_the_queue(store):
    """Focus arriving during the awaited snapshot cancels nothing."""
    rig = Rig(store, "p1")
    (queued,) = await _queue_idle(rig, "p1", "later")
    rig.state("p1").native_turn_id = "busy-turn"
    await asyncio.sleep(0)  # drain the admission-scheduled dispatch pass
    runtime = rig.runtimes["p1"]
    original = runtime.snapshot
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_snapshot():
        entered.set()
        await release.wait()
        return await original()

    runtime.snapshot = blocked_snapshot
    interrupt = asyncio.create_task(rig.service.interrupt("p1", caller_id="caller"))
    await entered.wait()
    rig.presence.provider = PresentPresence()
    release.set()

    with pytest.raises(HumanPresent):
        await interrupt
    assert store.get_job(queued).state == "running"
    assert store.queued_control_operation_count("p1") == 1
    kinds = {row["kind"] for row in _operation_rows(store, "p1")}
    assert "interrupt" not in kinds
    assert rig.state("p1").interrupted == []


async def _arm_durable_barrier(rig: Rig, pid: str, monkeypatch) -> None:
    """Leave one persisted operation with an active execution barrier."""
    runtime = rig.runtimes[pid]
    original_send = runtime.send

    async def uncertain(*, operation_id: str, prompt: str):
        raise RuntimeError("transport lost before acknowledgement")

    monkeypatch.setattr(runtime, "send", uncertain)
    await rig.service.send(pid, caller_id="caller", prompt="first")
    monkeypatch.setattr(runtime, "send", original_send)
    assert rig.store.has_execution_barrier(pid) is True


def _expire_legacy_claim(store, jobs, pid: str, monkeypatch) -> None:
    """An expired legacy claim a wrong-order busy check would crash."""
    from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS
    from theater.daemon.runtime import control_gates as control_gates_mod
    from theater.models import now as real_now

    jobs.create(handle=f"{pid}#claim", caller_id="caller", target_id=pid, kind="send", prompt="old")
    monkeypatch.setattr(control_gates_mod, "now", lambda: real_now() + SEND_CLAIM_TTL_SECONDS + 1)


async def test_present_focus_never_clears_a_durable_barrier_on_send(store, monkeypatch):
    """A send refused by presence leaves the execution barrier untouched."""
    rig = Rig(store, "p1")
    await _arm_durable_barrier(rig, "p1", monkeypatch)
    runtime = rig.runtimes["p1"]
    original = runtime.snapshot
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_snapshot():
        entered.set()
        await release.wait()
        return await original()

    runtime.snapshot = blocked_snapshot
    send = asyncio.create_task(rig.service.send("p1", caller_id="caller", prompt="second"))
    await entered.wait()
    rig.presence.provider = PresentPresence()
    release.set()

    with pytest.raises(HumanPresent):
        await send
    assert rig.store.has_execution_barrier("p1") is True
    assert rig.state("p1").sent == []
    assert len(_operation_rows(store, "p1")) == 1

    # Departure lets the legitimate idle path clear the barrier and deliver.
    rig.presence.provider = AbsentPresence()
    await rig.service.send("p1", caller_id="caller", prompt="second")
    assert rig.store.has_execution_barrier("p1") is False
    assert rig.state("p1").sent == ["second"]
    assert len(_operation_rows(store, "p1")) == 2


async def test_present_focus_never_clears_a_durable_barrier_on_settings(store, monkeypatch):
    """A settings update refused by presence leaves the barrier untouched."""
    rig = Rig(store, "p1")
    await _arm_durable_barrier(rig, "p1", monkeypatch)
    runtime = rig.runtimes["p1"]
    original = runtime.snapshot
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_snapshot():
        entered.set()
        await release.wait()
        return await original()

    runtime.snapshot = blocked_snapshot
    update = asyncio.create_task(
        rig.service.update_settings("p1", caller_id="caller", model="some-model")
    )
    await entered.wait()
    rig.presence.provider = PresentPresence()
    release.set()

    with pytest.raises(HumanPresent):
        await update
    assert rig.store.has_execution_barrier("p1") is True
    assert rig.state("p1").settings == {}


async def test_present_focus_retains_the_head_when_capabilities_fail(store):
    """A presence refusal outranks a capability failure: the head survives."""
    rig = Rig(store, "p1")
    (handle,) = await _queue_idle(rig, "p1", "later")
    rig.state("p1").native_turn_id = "busy-turn"
    await asyncio.sleep(0)  # drain the admission-scheduled dispatch pass
    rig.state("p1").unavailable = {
        RuntimeCapability.SEND: CapabilityUnavailableReason.GATED_BY_BACKEND
    }
    runtime = rig.runtimes["p1"]
    original = runtime.snapshot
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_snapshot():
        entered.set()
        await release.wait()
        return await original()

    runtime.snapshot = blocked_snapshot
    dispatch = asyncio.create_task(rig.service.dispatch_queue("p1"))
    await entered.wait()
    rig.presence.provider = PresentPresence()
    release.set()

    outcome = await dispatch
    assert outcome.deferred is True
    assert store.queued_control_operation_count("p1") == 1
    assert store.get_job(handle).state == "running"
    kinds = {row["kind"] for row in _operation_rows(store, "p1")}
    assert kinds == {"queue_followup"}

    # Departure classifies the capability failure; only then may the head fail.
    rig.presence.provider = AbsentPresence()
    outcome = await rig.service.dispatch_queue("p1")
    assert outcome.failed is not None
    assert store.get_job(handle).state == "crashed"
    assert store.queued_control_operation_count("p1") == 0


async def test_preflight_failure_never_fails_a_protected_queue(store):
    """A preflight error under present focus defers; absence classifies it."""
    from theater.models import StaleTarget

    rig = Rig(store, "p1")
    (handle,) = await _queue_idle(rig, "p1", "later")
    rig.state("p1").native_turn_id = "busy-turn"
    await asyncio.sleep(0)  # drain the admission-scheduled dispatch pass
    rig.presence.preflight_exception = StaleTarget("pane %1 of 'p1' no longer exists")

    dispatch = asyncio.create_task(rig.service.dispatch_queue("p1"))
    await rig.presence.preflight_parked.wait()
    rig.presence.provider = PresentPresence()
    outcome = await dispatch
    assert outcome.deferred is True
    assert store.queued_control_operation_count("p1") == 1
    assert store.get_job(handle).state == "running"

    # Departure classifies the same preflight error; only then may the head fail.
    rig.presence.provider = AbsentPresence()
    outcome = await rig.service.dispatch_queue("p1")
    assert outcome.failed is not None
    assert store.get_job(handle).state == "crashed"
    assert store.queued_control_operation_count("p1") == 0


async def test_legacy_dispatch_defers_on_presence_and_copy_mode(store):
    """Legacy delivery gates presence and copy mode as temporary deferrals."""
    rig = Rig(store, "legacy-1")
    rig.runtimes.pop("legacy-1")
    delivered: list[tuple[str, str]] = []

    async def deliver(participant_id: str, prompt: str) -> None:
        delivered.append((participant_id, prompt))

    rig.presence.gates()  # build once to prove the seam composes
    gates = ControlGates(
        authorize=lambda *args: None,
        require_absent=rig.presence.require_absent,
        send_preflight=_async_noop,
        legacy_copy_mode_check=_copy_mode_refusing({"legacy-1"}),
        legacy_busy_check=_async_noop,
        check_prompt=lambda prompt: None,
        check_settings=lambda model, effort: None,
        cwd_for=lambda participant_id: None,
        legacy_deliver=deliver,
    )
    service = ControlService(store=store, jobs=rig.jobs, runtime_for=lambda pid: None, gates=gates)
    _OWNED_SERVICES.append(service)
    job = await service.queue_followup("legacy-1", caller_id="caller", prompt="one")
    assert (await service.dispatch_queue("legacy-1")).deferred is True
    assert delivered == []
    assert store.queued_control_operation_count("legacy-1") == 1

    # Copy mode refuses as a temporary deferral too: queue and job intact.
    assert (await service.dispatch_queue("legacy-1")).deferred is True
    assert delivered == []
    assert store.queued_control_operation_count("legacy-1") == 1

    assert store.get_job(job.handle).state == "running"


async def test_legacy_send_rereads_working_after_awaited_prep(store, monkeypatch):
    """WORKING set during the awaited copy query refuses before claim handling."""
    from types import SimpleNamespace

    from theater.daemon.registry import Registry
    from theater.daemon.runtime import control_gates as control_gates_mod

    jobs = JobManager(store)
    registry = Registry(store)
    target = registry.create_spawned(harness="vibe", cwd="/tmp")
    delivered: list[tuple[str, str]] = []

    async def copy_mode_then_working(participant_id: str) -> None:
        await asyncio.sleep(0)  # the suspension the observer races
        registry.set_status(participant_id, Status.WORKING)

    async def deliver(participant_id: str, prompt: str) -> None:
        delivered.append((participant_id, prompt))

    daemon_like = SimpleNamespace(registry=registry, store=store, jobs=jobs)
    service = ControlService(
        store=store,
        jobs=jobs,
        runtime_for=lambda pid: None,
        gates=ControlGates(
            authorize=lambda *args: None,
            require_absent=_async_noop,
            send_preflight=_async_noop,
            legacy_copy_mode_check=copy_mode_then_working,
            legacy_busy_check=control_gates_mod._legacy_busy_check(daemon_like),
            check_prompt=lambda prompt: None,
            check_settings=lambda model, effort: None,
            cwd_for=lambda participant_id: None,
            legacy_deliver=deliver,
        ),
    )
    _OWNED_SERVICES.append(service)
    _expire_legacy_claim(store, jobs, target.id, monkeypatch)

    with pytest.raises(Busy):
        await service.send(target.id, caller_id="caller", prompt="must refuse")

    assert delivered == []
    assert _operation_rows(store, target.id) == []
    assert store.get_job(f"{target.id}#claim").state == "running"


async def test_legacy_send_presence_precedes_claim_handling(store, monkeypatch):
    """A human arriving during the copy query never triggers claim handling."""
    from types import SimpleNamespace

    from theater.daemon.registry import Registry
    from theater.daemon.runtime import control_gates as control_gates_mod

    jobs = JobManager(store)
    registry = Registry(store)
    target = registry.create_spawned(harness="vibe", cwd="/tmp")
    delivered: list[tuple[str, str]] = []
    presence = PresenceGates()

    async def copy_mode_then_human(participant_id: str) -> None:
        await asyncio.sleep(0)  # the suspension the observer races
        presence.provider = PresentPresence()

    async def deliver(participant_id: str, prompt: str) -> None:
        delivered.append((participant_id, prompt))

    daemon_like = SimpleNamespace(registry=registry, store=store, jobs=jobs)
    service = ControlService(
        store=store,
        jobs=jobs,
        runtime_for=lambda pid: None,
        gates=ControlGates(
            authorize=lambda *args: None,
            require_absent=presence.require_absent,
            send_preflight=_async_noop,
            legacy_copy_mode_check=copy_mode_then_human,
            legacy_busy_check=control_gates_mod._legacy_busy_check(daemon_like),
            check_prompt=lambda prompt: None,
            check_settings=lambda model, effort: None,
            cwd_for=lambda participant_id: None,
            legacy_deliver=deliver,
        ),
    )
    _OWNED_SERVICES.append(service)
    _expire_legacy_claim(store, jobs, target.id, monkeypatch)

    with pytest.raises(HumanPresent):
        await service.send(target.id, caller_id="caller", prompt="must refuse")

    assert delivered == []
    assert _operation_rows(store, target.id) == []
    assert store.get_job(f"{target.id}#claim").state == "running"


async def _async_noop(*args, **kwargs) -> None:
    return None


def _copy_mode_refusing(targets: set[str]):
    async def check(participant_id: str) -> None:

        if participant_id in targets:
            raise Busy(f"pane of {participant_id!r} is in copy mode")

    return check


async def test_unrelated_participant_is_never_stalled_by_a_blocked_gate(store):
    """One participant's blocked presence check never stalls another's send."""
    rig = Rig(store, "p1", "p2")
    await rig.service.send("p1", caller_id="caller", prompt="first")
    await _queue_idle(rig, "p1", "later")

    release = asyncio.Event()

    async def blocking_require_absent(participant_id: str) -> None:
        if participant_id == "p1":
            await release.wait()
        await rig.presence.require_absent(participant_id)

    # ControlGates is frozen with slots, so compose a fresh service instead.
    blocked_service = ControlService(
        store=store,
        jobs=rig.jobs,
        runtime_for=rig.runtimes.get,
        gates=ControlGates(
            authorize=lambda *args: None,
            require_absent=blocking_require_absent,
            send_preflight=_async_noop,
            legacy_copy_mode_check=_async_noop,
            legacy_busy_check=_async_noop,
            check_prompt=lambda prompt: None,
            check_settings=lambda model, effort: None,
            cwd_for=lambda participant_id: None,
            legacy_deliver=_async_noop,
        ),
    )
    _OWNED_SERVICES.append(blocked_service)
    dispatch = asyncio.create_task(blocked_service.dispatch_queue("p1"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # p2's ordinary send completes while p1's dispatch pass is parked.
    sent = await rig.service.send("p2", caller_id="caller", prompt="other")

    assert sent.state == "running"
    assert rig.state("p2").sent == ["other"]

    release.set()
    outcome = await dispatch
    assert outcome.dispatched or outcome.deferred


# ---- composed daemon: agent mutations ---------------------------------------


async def _hello_target(client, daemon, *, pane="%1"):
    target = await client.call("hello", harness="vibe", pane=pane, cwd="/tmp")
    participant = daemon.registry.get(target["id"])
    participant.session_id = f"session-{target['id']}"
    participant.session_correlation = "operator"
    daemon.store.upsert_participant(participant)
    return target


async def _native_child(client, daemon, fake_tmux, *, pane="%9", pid=4242):
    fake_tmux.add_pane(pane, command="vibe", pid=pid)
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    daemon.registry.attach_pane(child.id, pane, pane_pid=pid)
    state = FakeRuntimeState(participant_id=child.id, backend_generation=1)
    state.native_session_id = "thread-1"
    state.backend_alive = True
    context = RuntimeContext(
        participant_id=child.id,
        cwd="/tmp",
        io=FakeRuntimeIO(state),
        backend_generation=1,
    )

    async def create():
        return FakeRuntime(context)

    await daemon.runtime_manager.get_or_create(child.id, backend_generation=1, create=create)
    return parent, daemon.registry.get(child.id), state


async def _working_legacy_child(daemon, fake_tmux, *, pane="%1", pid=4242):
    """A working pane-wired child a legacy interrupt would target."""
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    fake_tmux.add_pane(pane, command="vibe", pid=pid)
    daemon.registry.attach_pane(child.id, pane, pane_pid=pid)
    daemon.registry.set_status(child.id, Status.WORKING)
    return parent, daemon.registry.get(child.id)


def _interrupt_events(daemon) -> list[dict]:
    """Interrupt-requested bus events, newest last."""
    return [
        event
        for event in daemon.store.bus_tail(limit=100)
        if event["kind"] == BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED
    ]


def _kill_events(daemon) -> list[dict]:
    """Kill-requested bus events, newest last."""
    return [
        event
        for event in daemon.store.bus_tail(limit=100)
        if event["kind"] == BUS_KIND_PARTICIPANT_KILL_REQUESTED
    ]


async def test_composed_send_refused_while_present_and_recorded(client, daemon, fake_tmux):
    target = await _hello_target(client, daemon)
    daemon.presence = PresentPresence()

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="hi")

    assert exc.value.code == "human_present"
    assert fake_tmux.sent == []
    refusals = [e for e in daemon.store.bus_tail(limit=100) if e["kind"] == BUS_KIND_SEND_REFUSED]
    assert refusals[-1]["payload"]["reason"] == "human_present"

    daemon.presence.set_state(PresenceState.ABSENT, "human left")
    job = await client.call("send", target=target["id"], prompt="hi")
    assert job["state"] == "running"
    assert fake_tmux.sent == [("%1", "hi")]


async def test_cli_and_agent_callers_face_the_same_present_gate(client, daemon, fake_tmux):
    _parent, child, _state = await _native_child(client, daemon, fake_tmux)
    daemon.presence = PresentPresence()

    for caller_id in (None, "some-parent"):
        params = {"target": child.id, "prompt": "hi"}
        if caller_id is not None:
            params["caller_id"] = caller_id
        with pytest.raises(RemoteError) as exc:
            await client.call("send", **params)
        assert exc.value.code == "human_present"
    assert _state_sent_count(daemon, child.id) == 0


def _state_sent_count(daemon, pid: str) -> int:
    runtime = daemon.runtime_manager.get(pid)
    assert runtime is not None
    return len(runtime.state.sent)


async def test_kill_and_metadata_refused_while_present(client, daemon, fake_tmux):
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    fake_tmux.add_pane("%1", command="vibe", pid=4242)
    daemon.registry.attach_pane(child.id, "%1", pane_pid=4242)
    daemon.registry.set_status(child.id, Status.WORKING)
    daemon.presence = PresentPresence()

    with pytest.raises(RemoteError) as exc:
        await client.call("participant.kill", id=child.id, caller_id=parent.id)
    assert exc.value.code == "human_present"
    assert daemon.registry.get(child.id).status is Status.WORKING
    kill_events = [
        e
        for e in daemon.store.bus_tail(limit=100)
        if e["kind"] == BUS_KIND_PARTICIPANT_KILL_REQUESTED
    ]
    assert kill_events == []

    with pytest.raises(RemoteError) as rename:
        await client.call("participant.rename", id=child.id, name="renamed")
    assert rename.value.code == "human_present"
    assert daemon.registry.get(child.id).name != "renamed"

    with pytest.raises(RemoteError) as update:
        await client.call(
            "participant.update",
            caller_id=parent.id,
            target=child.id,
            description="new",
        )
    assert update.value.code == "human_present"
    assert daemon.registry.get(child.id).description != "new"

    with pytest.raises(RemoteError) as status:
        await client.call("participant.status", id=child.id, status="idle")
    assert status.value.code == "human_present"
    assert daemon.registry.get(child.id).status is Status.WORKING

    # Departure releases every one of the same mutations.
    daemon.presence.set_state(PresenceState.ABSENT, "human left")
    renamed = await client.call("participant.rename", id=child.id, name="renamed")
    assert renamed["id"] == child.id
    assert daemon.registry.get(child.id).name == "renamed"


async def test_adopting_a_live_participants_pane_refused_while_present(client, daemon, fake_tmux):
    target = await _hello_target(client, daemon)
    daemon.presence = PresentPresence()

    with pytest.raises(RemoteError) as exc:
        await client.call("adopt", pane="%1")

    assert exc.value.code == "human_present"
    assert daemon.store.find_by_pane("%1").id == target["id"]


async def test_missing_provider_never_grants_absence(client, daemon, fake_tmux):
    """No composed provider: mutations fail closed and reads project unknown."""
    target = await _hello_target(client, daemon)

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="hi")
    assert exc.value.code == "human_present"
    assert "no composed presence provider" in exc.value.message
    assert fake_tmux.sent == []

    record = await client.call("participants.get", id=target["id"])
    assert record["human_presence"] == {
        "state": "unknown",
        "protected": True,
        "reason": "presence provider not composed",
        "revision": 0,
        "observed_at": None,
    }
    controls = await client.call("participant.controls", target=target["id"])
    assert controls["human_presence"]["protected"] is True
    assert controls["human_presence"]["state"] == "unknown"


async def test_wire_reads_carry_the_shared_presence_projection(client, daemon, fake_tmux):
    target = await _hello_target(client, daemon)
    daemon.presence = UnknownPresence()
    expected = daemon.presence.snapshot(target["id"]).to_dict()

    record = await client.call("participants.get", id=target["id"])
    assert record["human_presence"] == expected
    assert set(record["human_presence"]) == {
        "state",
        "protected",
        "reason",
        "revision",
        "observed_at",
    }

    listing = await client.call("participants.list")
    assert [row["human_presence"] for row in listing] == [expected]

    tree = await client.call("participants.tree")
    assert [node["human_presence"] for node in tree] == [expected]

    daemon.presence = AbsentPresence()
    absent_expected = daemon.presence.snapshot(target["id"]).to_dict()
    controls = await client.call("participant.controls", target=target["id"])
    assert controls["human_presence"] == absent_expected


async def test_copy_mode_blocks_legacy_delivery_but_not_native_controls(
    client, daemon, fake_tmux, monkeypatch
):
    from theater.daemon.rpc import sending as sending_mod

    async def in_copy_mode(pane_id):
        return True

    monkeypatch.setattr(sending_mod, "human_present", in_copy_mode)

    _parent, child, state = await _native_child(client, daemon, fake_tmux)
    daemon.presence = AbsentPresence()
    job = await client.call("send", target=child.id, prompt="native is safe")
    assert job["state"] == "running"
    assert state.sent == ["native is safe"]

    legacy = await _hello_target(client, daemon, pane="%2")
    with pytest.raises(RemoteError) as legacy_send:
        await client.call("send", target=legacy["id"], prompt="keys are not")
    assert legacy_send.value.code == "busy"
    assert "copy mode" in legacy_send.value.message
    assert fake_tmux.sent == []

    daemon.registry.set_status(legacy["id"], Status.WORKING)
    with pytest.raises(RemoteError) as legacy_interrupt:
        await client.call("participant.interrupt", target=legacy["id"], caller_id="p-parent")
    assert legacy_interrupt.value.code in {"busy", "not_your_child"}


async def test_disconnected_native_with_present_focus_refuses_without_fallback(
    client, daemon, fake_tmux, monkeypatch
):
    from theater.tmux import client as tmux

    parent, child = (
        daemon.registry.create_spawned(harness="vibe", cwd="/tmp"),
        None,
    )
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    fake_tmux.add_pane("%1", command="vibe", pid=4242)
    daemon.registry.attach_pane(child.id, "%1", pane_pid=4242)
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=child.id,
            harness="vibe",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.DETACHED,
            native_session_id="thread-gone",
        )
    )
    daemon.presence = PresentPresence()

    async def unexpected_delivery(*args, **kwargs):
        raise AssertionError("a protected pane must never receive keys")

    monkeypatch.setattr(tmux, "deliver_keys", unexpected_delivery)
    monkeypatch.setattr(tmux, "deliver_text", unexpected_delivery)

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=child.id, prompt="hi", caller_id=parent.id)
    assert exc.value.code == "human_present"


async def test_focus_arriving_during_copy_query_refuses_legacy_send(
    client, daemon, fake_tmux, monkeypatch
):
    """The recheck after the awaited copy query refuses before delivery."""
    from theater.daemon.rpc import sending as sending_mod

    target = await _hello_target(client, daemon)
    daemon.presence = AbsentPresence()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_copy_query(pane_id):
        entered.set()
        await release.wait()
        return False

    monkeypatch.setattr(sending_mod, "human_present", blocked_copy_query)
    send = asyncio.create_task(client.call("send", target=target["id"], prompt="hi"))
    await entered.wait()
    daemon.presence.set_state(PresenceState.PRESENT, "human arrived")
    release.set()

    with pytest.raises(RemoteError) as exc:
        await send
    assert exc.value.code == "human_present"
    assert fake_tmux.sent == []
    assert daemon.store.running_jobs_for_target(target["id"]) == []

    # Departure releases the same send; nothing was minted or delivered.
    daemon.presence.set_state(PresenceState.ABSENT, "human left")
    job = await client.call("send", target=target["id"], prompt="hi")
    assert job["state"] == "running"
    assert ("%1", "hi") in fake_tmux.sent


async def test_legacy_send_rereads_activity_after_the_final_presence_refresh(
    client, daemon, fake_tmux, monkeypatch
):
    """WORKING set during the final presence refresh still refuses the send."""
    from theater.daemon.rpc import sending as sending_mod

    target = await _hello_target(client, daemon)
    daemon.presence = AbsentPresence()
    entered, release = asyncio.Event(), asyncio.Event()
    original = sending_mod.control_gates.require_absent
    calls = {"count": 0}

    async def blocked_second_refresh(daemon_, participant_id):
        calls["count"] += 1
        if calls["count"] >= 2:
            entered.set()
            await release.wait()
        await original(daemon_, participant_id)

    monkeypatch.setattr(sending_mod.control_gates, "require_absent", blocked_second_refresh)
    send = asyncio.create_task(client.call("send", target=target["id"], prompt="hi"))
    await entered.wait()
    daemon.registry.set_status(target["id"], Status.WORKING)
    release.set()

    with pytest.raises(RemoteError) as exc:
        await send
    assert exc.value.code == "busy"
    assert fake_tmux.sent == []
    assert daemon.store.running_jobs_for_target(target["id"]) == []


async def test_composed_legacy_dispatch_rereads_working_after_prep(
    client, daemon, fake_tmux, monkeypatch
):
    """WORKING set during the queued dispatch's copy query defers the head."""
    from theater.daemon.rpc import sending as sending_mod

    target = await _hello_target(client, daemon)
    daemon.presence = AbsentPresence()
    _expire_legacy_claim(daemon.store, daemon.jobs, target["id"], monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_copy_query(pane_id):
        entered.set()
        await release.wait()
        daemon.registry.set_status(target["id"], Status.WORKING)
        return False

    monkeypatch.setattr(sending_mod, "human_present", blocked_copy_query)
    await client.call(
        "participant.queue_followup", target=target["id"], prompt="later", caller_id="cli"
    )
    # The admission-scheduled pass parks inside the copy-mode query.
    await entered.wait()
    release.set()

    outcome = await daemon.controls.dispatch_queue(target["id"])
    assert outcome.deferred is True
    assert daemon.store.queued_control_operation_count(target["id"]) == 1
    assert daemon.store.get_job(f"{target['id']}#claim").state == "running"
    assert fake_tmux.sent == []
    rows = _operation_rows(daemon.store, target["id"])
    assert all(row["delivery_phase"] == "queued" for row in rows)


async def test_composed_legacy_dispatch_presence_keeps_claims_and_fifo(
    client, daemon, fake_tmux, monkeypatch
):
    """A human arriving during the dispatch's copy query keeps claims and FIFO."""
    from theater.daemon.rpc import sending as sending_mod

    target = await _hello_target(client, daemon)
    daemon.presence = AbsentPresence()
    _expire_legacy_claim(daemon.store, daemon.jobs, target["id"], monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    flipped = {"done": False}

    async def blocked_copy_query(pane_id):
        entered.set()
        await release.wait()
        if not flipped["done"]:
            flipped["done"] = True
            daemon.presence.set_state(PresenceState.PRESENT, "human arrived")
        return False

    monkeypatch.setattr(sending_mod, "human_present", blocked_copy_query)
    await client.call(
        "participant.queue_followup", target=target["id"], prompt="later", caller_id="cli"
    )
    # The admission-scheduled pass parks inside the copy-mode query.
    await entered.wait()
    release.set()

    outcome = await daemon.controls.dispatch_queue(target["id"])
    assert outcome.deferred is True
    assert daemon.store.queued_control_operation_count(target["id"]) == 1
    assert daemon.store.get_job(f"{target['id']}#claim").state == "running"
    assert fake_tmux.sent == []

    # Departure releases the unchanged FIFO head.
    daemon.presence.set_state(PresenceState.ABSENT, "human left")
    outcome = await daemon.controls.dispatch_queue(target["id"])
    assert outcome.dispatched is not None
    assert fake_tmux.sent == [("%1", "later")]


async def test_focus_arriving_during_native_snapshot_refuses_send(
    client, daemon, fake_tmux, monkeypatch
):
    """The recheck after the awaited native snapshot refuses before minting."""
    _parent, child, state = await _native_child(client, daemon, fake_tmux)
    daemon.presence = AbsentPresence()
    runtime = daemon.runtime_manager.get(child.id)
    original = runtime.snapshot
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_snapshot():
        entered.set()
        await release.wait()
        return await original()

    monkeypatch.setattr(runtime, "snapshot", blocked_snapshot)
    send = asyncio.create_task(client.call("send", target=child.id, prompt="hi"))
    await entered.wait()
    daemon.presence.set_state(PresenceState.PRESENT, "human arrived")
    release.set()

    with pytest.raises(RemoteError) as exc:
        await send
    assert exc.value.code == "human_present"
    assert _operation_rows(daemon.store, child.id) == []
    assert daemon.store.running_jobs_for_target(child.id) == []
    assert state.sent == []


async def test_focus_arriving_during_copy_query_refuses_legacy_interrupt(
    client, daemon, fake_tmux, monkeypatch
):
    """The recheck after the awaited copy query refuses before injection."""
    from theater.daemon.rpc import sending as sending_mod
    from theater.tmux import client as tmux

    parent, child = await _working_legacy_child(daemon, fake_tmux)
    daemon.presence = AbsentPresence()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_copy_query(pane_id):
        entered.set()
        await release.wait()
        return False

    monkeypatch.setattr(sending_mod, "human_present", blocked_copy_query)

    async def unexpected_keys(*args, **kwargs):
        raise AssertionError("a protected pane must never receive keys")

    monkeypatch.setattr(tmux, "deliver_keys", unexpected_keys)
    interrupt = asyncio.create_task(
        client.call("participant.interrupt", target=child.id, caller_id=parent.id)
    )
    await entered.wait()
    daemon.presence.set_state(PresenceState.PRESENT, "human arrived")
    release.set()

    with pytest.raises(RemoteError) as exc:
        await interrupt
    assert exc.value.code == "human_present"
    # No status mutation and no interrupt event: the child is as it was.
    assert daemon.registry.get(child.id).status is Status.WORKING
    assert _interrupt_events(daemon) == []


async def test_focus_arriving_during_detection_refuses_adopt(
    client, daemon, fake_tmux, monkeypatch
):
    """The recheck after the awaited detection protects the register."""
    from theater.daemon.rpc import participants as participants_mod

    target = await _hello_target(client, daemon)
    daemon.presence = AbsentPresence()
    entered, release = asyncio.Event(), asyncio.Event()
    original = participants_mod.detect_harness_async

    async def blocked_detection(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(participants_mod, "detect_harness_async", blocked_detection)
    adopt = asyncio.create_task(client.call("adopt", pane="%1"))
    await entered.wait()
    daemon.presence.set_state(PresenceState.PRESENT, "human arrived")
    release.set()

    with pytest.raises(RemoteError) as exc:
        await adopt
    assert exc.value.code == "human_present"
    owner = daemon.store.find_by_pane("%1")
    assert owner.id == target["id"]
    assert owner.status is not Status.DEAD


async def test_focus_arriving_during_reconcile_refuses_kill(client, daemon, fake_tmux, monkeypatch):
    """The recheck after the awaited reconcile refuses immediately pre-kill."""
    from theater.daemon.rpc import participants as participants_mod

    parent, child = await _working_legacy_child(daemon, fake_tmux)
    daemon.presence = AbsentPresence()
    entered, release = asyncio.Event(), asyncio.Event()
    original = participants_mod.reconcile_tmux_inventory_locked

    async def blocked_reconcile(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(participants_mod, "reconcile_tmux_inventory_locked", blocked_reconcile)
    kill = asyncio.create_task(client.call("participant.kill", id=child.id, caller_id=parent.id))
    await entered.wait()
    daemon.presence.set_state(PresenceState.PRESENT, "human arrived")
    release.set()

    with pytest.raises(RemoteError) as exc:
        await kill
    assert exc.value.code == "human_present"
    assert daemon.registry.get(child.id).status is Status.WORKING
    assert _kill_events(daemon) == []


# ---- copy-mode query behavior ------------------------------------------------


async def test_copy_mode_query_errors_refuse_instead_of_failing_open(monkeypatch):
    from theater.daemon.rpc import sending as sending_mod

    async def broken(pane_id):
        raise RuntimeError("tmux did not answer")

    monkeypatch.setattr(sending_mod, "human_present", broken)
    refusal = await sending_mod.copy_mode_refusal("%1")
    assert refusal is not None
    assert refusal.code == "busy"
    assert "could not be verified" in str(refusal)
