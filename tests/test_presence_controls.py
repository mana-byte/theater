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
from theater.daemon.presence import PresenceState
from theater.daemon.schema import control_operations as control_operations_table
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    RuntimeCapability,
    RuntimeContext,
)
from theater.models import HumanPresent, Status
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
        self.check_absent(participant_id)

    def check_absent(self, participant_id: str) -> None:
        if self.provider is not None:
            if self.provider.snapshot(participant_id).protected:
                raise HumanPresent(f"human focus protects {participant_id!r}")
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
            check_absent=self.check_absent,
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


async def _async_noop(*args, **kwargs) -> None:
    return None


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
            check_absent=rig.presence.check_absent,
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


async def _hello_target(client, daemon, *, pane=None):
    target = await client.call("hello", harness="vibe", pane=pane, cwd="/tmp")
    participant = daemon.registry.get(target["id"])
    participant.session_id = f"session-{target['id']}"
    participant.session_correlation = "operator"
    daemon.store.upsert_participant(participant)
    return target


async def _native_child(client, daemon, terminal_provider, *, pane=None, pid=4242):
    del client, pane, pid
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    terminal_provider.bind(daemon, child.id)
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


def _interrupt_events(daemon) -> list[dict]:
    """Interrupt-requested bus events, newest last."""
    return [
        event
        for event in daemon.store.bus_tail(limit=100)
        if event["kind"] == BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED
    ]


async def test_composed_send_refused_while_present_and_recorded(client, daemon, terminal_provider):
    target = await _hello_target(client, daemon)
    terminal_id = terminal_provider.bind(daemon, target["id"])
    daemon.presence = PresentPresence()

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="hi")

    assert exc.value.code == "human_present"
    assert terminal_provider.deliveries == []
    refusals = [e for e in daemon.store.bus_tail(limit=100) if e["kind"] == BUS_KIND_SEND_REFUSED]
    assert refusals[-1]["payload"]["reason"] == "human_present"

    daemon.presence.set_state(PresenceState.ABSENT, "human left")
    job = await client.call("send", target=target["id"], prompt="hi")
    assert job["state"] == "running"
    assert terminal_provider.deliveries == [(terminal_id, "hi")]


async def test_cli_and_agent_callers_face_the_same_present_gate(client, daemon, terminal_provider):
    _parent, child, _state = await _native_child(client, daemon, terminal_provider)
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


async def test_kill_and_status_refused_metadata_allowed_while_present(
    client, daemon, terminal_provider
):
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    terminal_provider.bind(daemon, child.id)
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

    renamed = await client.call("participant.rename", id=child.id, name="renamed")
    assert renamed["id"] == child.id
    assert daemon.registry.get(child.id).name == "renamed"

    await client.call(
        "participant.update",
        caller_id=parent.id,
        target=child.id,
        description="new",
    )
    assert daemon.registry.get(child.id).description == "new"

    with pytest.raises(RemoteError) as status:
        await client.call("participant.status", id=child.id, status="idle")
    assert status.value.code == "human_present"
    assert daemon.registry.get(child.id).status is Status.WORKING

    daemon.presence.set_state(PresenceState.ABSENT, "human left")
    await client.call("participant.status", id=child.id, status="idle")
    assert daemon.registry.get(child.id).status is Status.IDLE


async def test_missing_provider_never_grants_absence(client, daemon, terminal_provider):
    """No composed provider: mutations fail closed and reads project unknown."""
    target = await _hello_target(client, daemon)
    terminal_provider.bind(daemon, target["id"])
    daemon.presence = None

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="hi")
    assert exc.value.code == "human_present"
    assert "no composed presence provider" in exc.value.message
    assert terminal_provider.deliveries == []

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


async def test_wire_reads_carry_the_shared_presence_projection(client, daemon, terminal_provider):
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


async def test_focus_arriving_during_native_snapshot_refuses_send(
    client, daemon, terminal_provider, monkeypatch
):
    """The recheck after the awaited native snapshot refuses before minting."""
    _parent, child, state = await _native_child(client, daemon, terminal_provider)
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
