"""Composed focus-protection tests across the daemon control surfaces.

Every agent-requested mutation of a focus-protected target refuses before
durable effects; copy mode is a separate legacy-delivery refusal, and the
read-only wire carries the shared human_presence projection.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from tests._presence_doubles import AbsentPresence, PresentPresence, UnknownPresence
from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.constants.daemon import (
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
    RuntimeContext,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import HumanPresent, Status
from theater.protocol import RemoteError

# ---- service-level rig ------------------------------------------------------


class PresenceGates:
    """A ControlGates presence seam with per-target scripted refusals."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        #: Targets whose presence gate always refuses.
        self.refusals: set[str] = set()
        #: Targets refused only after N passing calls (focus arrives mid-flight).
        self.passes_before_refusal: dict[str, int] = {}

    async def require_absent(self, participant_id: str) -> None:
        calls = self.calls.get(participant_id, 0) + 1
        self.calls[participant_id] = calls
        passes = self.passes_before_refusal.get(participant_id)
        if participant_id in self.refusals or (
            passes is not None and calls > passes
        ):
            raise HumanPresent(f"human focus protects {participant_id!r}")

    def gates(self) -> ControlGates:
        async def noop(*args, **kwargs) -> None:
            return None

        return ControlGates(
            authorize=lambda *args: None,
            require_absent=self.require_absent,
            send_preflight=noop,
            legacy_copy_mode_check=noop,
            legacy_busy_check=noop,
            check_prompt=lambda prompt: None,
            check_settings=lambda model, effort: None,
            cwd_for=lambda participant_id: None,
            legacy_deliver=noop,
        )


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

    def state(self, pid: str) -> FakeRuntimeState:
        return self.runtimes[pid].state


def _operation_rows(store, pid: str) -> list[dict]:
    return [
        dict(row._mapping)
        for row in store.conn.execute(
            select(control_operations_table).where(
                control_operations_table.c.participant_id == pid
            )
        ).fetchall()
    ]


async def _queue_idle(rig: Rig, pid: str, *prompts: str) -> list[str]:
    """Queue followups that stay pending while the turn is busy, then idle."""
    rig.state(pid).native_turn_id = "busy-turn"
    jobs = []
    try:
        for prompt in prompts:
            jobs.append(
                (
                    await rig.service.queue_followup(
                        pid, caller_id="caller", prompt=prompt
                    )
                ).handle
            )
    finally:
        rig.state(pid).native_turn_id = None
    return jobs


# ---- service-level gates ----------------------------------------------------


async def test_send_refused_for_present_focus_mints_nothing(store):
    rig = Rig(store, "p1")
    rig.presence.refusals = {"p1"}

    with pytest.raises(HumanPresent):
        await rig.service.send("p1", caller_id="caller", prompt="hi")

    assert _operation_rows(store, "p1") == []
    assert store.active_running_jobs_for_target("p1") == []
    assert rig.state("p1").sent == []


async def test_send_refused_for_unknown_focus_mints_nothing(store):
    """Unknown focus facts protect exactly like a present human."""
    rig = Rig(store, "p1")
    rig.presence.refusals = {"p1"}

    with pytest.raises(HumanPresent):
        await rig.service.send("p1", caller_id="caller", prompt="hi")

    assert _operation_rows(store, "p1") == []


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
        await rig.service.update_settings(
            "p1", caller_id="caller", model="some-model"
        )

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
    service = ControlService(
        store=store, jobs=rig.jobs, runtime_for=lambda pid: None, gates=gates
    )
    job = await service.queue_followup("legacy-1", caller_id="caller", prompt="one")
    assert (await service.dispatch_queue("legacy-1")).deferred is True
    assert delivered == []
    assert store.queued_control_operation_count("legacy-1") == 1

    # Copy mode refuses as a temporary deferral too: queue and job intact.
    assert (await service.dispatch_queue("legacy-1")).deferred is True
    assert delivered == []
    assert store.queued_control_operation_count("legacy-1") == 1

    assert store.get_job(job.handle).state == "running"


async def _async_noop(*args, **kwargs) -> None:
    return None


def _copy_mode_refusing(targets: set[str]):
    async def check(participant_id: str) -> None:
        from theater.models import Busy

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

    await daemon.runtime_manager.get_or_create(
        child.id, backend_generation=1, create=create
    )
    return parent, daemon.registry.get(child.id), state


async def test_composed_send_refused_while_present_and_recorded(
    client, daemon, fake_tmux
):
    target = await _hello_target(client, daemon)
    daemon.presence = PresentPresence()

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="hi")

    assert exc.value.code == "human_present"
    assert fake_tmux.sent == []
    refusals = [
        e
        for e in daemon.store.bus_tail(limit=100)
        if e["kind"] == BUS_KIND_SEND_REFUSED
    ]
    assert refusals[-1]["payload"]["reason"] == "human_present"

    daemon.presence.set_state(PresenceState.ABSENT, "human left")
    job = await client.call("send", target=target["id"], prompt="hi")
    assert job["state"] == "running"
    assert fake_tmux.sent == [("%1", "hi")]


async def test_cli_and_agent_callers_face_the_same_present_gate(
    client, daemon, fake_tmux
):
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


async def test_adopting_a_live_participants_pane_refused_while_present(
    client, daemon, fake_tmux
):
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


async def test_wire_reads_carry_the_shared_presence_projection(
    client, daemon, fake_tmux
):
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
        await client.call(
            "participant.interrupt", target=legacy["id"], caller_id="p-parent"
        )
    assert legacy_interrupt.value.code in {"busy", "not_your_child"}


async def test_disconnected_native_with_present_focus_refuses_without_fallback(
    client, daemon, fake_tmux, monkeypatch
):
    from theater.tmux import client as tmux

    parent, child = (
        daemon.registry.create_spawned(harness="vibe", cwd="/tmp"),
        None,
    )
    child = daemon.registry.create_spawned(
        harness="vibe", cwd="/tmp", parent_id=parent.id
    )
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


async def test_focus_arriving_mid_send_refuses_before_any_delivery(
    client, daemon, fake_tmux
):
    """The composed recheck refuses after the awaited preflights, pre-delivery."""
    target = await _hello_target(client, daemon)
    daemon.presence = AbsentPresence()
    monkeypatched_second_call = {"count": 0}

    original = daemon.presence.require_absent

    async def flip_on_second(participant_id: str) -> None:
        monkeypatched_second_call["count"] += 1
        if monkeypatched_second_call["count"] >= 2:
            daemon.presence.set_state(PresenceState.PRESENT, "human arrived")
        await original(participant_id)

    daemon.presence.require_absent = flip_on_second

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="hi")

    assert exc.value.code == "human_present"
    assert fake_tmux.sent == []
    handles = [job.handle for job in daemon.store.running_jobs_for_target(target["id"])]
    assert handles == []


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
