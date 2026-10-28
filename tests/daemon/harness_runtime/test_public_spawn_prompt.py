"""The public spawn's initial prompt on a detached-native harness: once, and loud on refusal."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from tests.support.native_lifecycle import (
    NativeFixtureHarness,
    native_daemon,
    teardown_backend,
)
from tests.support.waiting import settle_operations
from theater.daemon.spawning import private_operations
from theater.daemon.spawning.provider_launch import ParticipantLaunchService
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ControlDeliveryPhase,
    DeliveryResult,
    RuntimeCapability,
    RuntimeSessionOrder,
)
from theater.models import BadRequest, JobState

pytestmark = pytest.mark.usefixtures("native_harness_binary")


class _SendRefusingHarness(NativeFixtureHarness):
    """A native fixture whose runtime gates SEND, so the initial prompt is refused."""

    def _manifest(self):
        manifest = super()._manifest()

        def factory(context):
            runtime = manifest.factory(context)
            runtime.state.unavailable[RuntimeCapability.SEND] = (
                CapabilityUnavailableReason.THEATER_POLICY
            )
            return runtime

        return replace(manifest, factory=factory)


class _UncertainDispatchHarness(NativeFixtureHarness):
    """A native fixture whose backend write is lost, recording its one attempt."""

    def _manifest(self):
        manifest = super()._manifest()

        def factory(context):
            runtime = manifest.factory(context)

            async def send(*, operation_id, prompt):
                self.events.append(("send", prompt))
                raise ConnectionError("the backend write was lost")

            runtime.send = send
            return runtime

        return replace(manifest, factory=factory)


async def _public_spawn(
    daemon, *, key: str, prompt: str | None, cwd, parent: str | None = None
) -> dict:
    """Spawn through the public launch service, the route a human client hits."""
    params: dict[str, object] = {
        "harness": "native-fixture",
        "approval": "manual",
        "cwd": str(cwd),
    }
    if prompt:
        params["prompt"] = prompt
    if parent is not None:
        params["initiating_participant_id"] = parent
    return await ParticipantLaunchService(daemon).spawn(
        client_id="operator-a", idempotency_key=key, params=params
    )


@pytest.mark.parametrize(
    "session_order",
    [RuntimeSessionOrder.FRONTEND_FIRST, RuntimeSessionOrder.SESSION_FIRST],
)
async def test_parentless_public_spawn_delivers_the_initial_prompt_once(
    session_order, native_rig, terminal_provider, tmp_path
) -> None:
    harness = NativeFixtureHarness()
    harness.runtime = replace(harness.runtime, session_order=session_order)
    d = await native_daemon(native_rig.io, harness, terminal_provider)
    spawned = []
    try:
        accepted = await _public_spawn(d, key="public-prompt", prompt="do the task", cwd=tmp_path)
        await settle_operations(d)
        pid = accepted["participant_id"]
        spawned.append(pid)

        # The prompt reached the runtime through the control service, exactly once.
        assert [e for e in harness.events if e[0] == "send"] == [("send", "do the task")]
        runtime = d.runtime_manager.get(pid)
        assert runtime.state.sent == ["do the task"]

        job = d.store.get_job(pid)
        # The reservation and the initial dispatch share one caller identity.
        assert job.caller_id == "cli"
        assert job.actor_client_id == "operator-a"
        assert job.actor_participant_id is None
        assert job.state == JobState.RUNNING
        (operation,) = d.store.control_operations_for_job(pid)
        assert operation.delivery_result is DeliveryResult.ACCEPTED
    finally:
        for p in spawned:
            await teardown_backend(d, p)
        await d.aclose()


async def test_rejected_initial_prompt_crashes_the_spawn_job(
    native_rig, terminal_provider, tmp_path
) -> None:
    harness = _SendRefusingHarness()
    d = await native_daemon(native_rig.io, harness, terminal_provider)
    spawned = []
    try:
        accepted = await _public_spawn(d, key="refused-prompt", prompt="do the task", cwd=tmp_path)
        await settle_operations(d)
        pid = accepted["participant_id"]
        spawned.append(pid)

        job = d.store.get_job(pid)
        assert job.state == JobState.CRASHED
        assert "does not support send" in (job.result or "")
        assert job.error_code == "bad_request"
        # Nothing was delivered: no control operation may exist for the spawn job.
        assert d.store.control_operations_for_job(pid) == []
    finally:
        for p in spawned:
            await teardown_backend(d, p)
        await d.aclose()


async def test_uncertain_initial_dispatch_never_becomes_a_refusal(
    native_rig, terminal_provider, tmp_path
) -> None:
    """A lost backend write keeps its operation and job for reconciliation; no crash, no replay."""
    harness = _UncertainDispatchHarness()
    d = await native_daemon(native_rig.io, harness, terminal_provider)
    spawned = []
    try:
        accepted = await _public_spawn(
            d, key="uncertain-prompt", prompt="do the task", cwd=tmp_path
        )
        await settle_operations(d)
        pid = accepted["participant_id"]
        spawned.append(pid)

        # Exactly one attempt, never retransmitted: its single operation stays
        # dispatched for reconciliation (maintenance may clear the barrier off an
        # idle snapshot), and the job is never crashed.
        assert [e for e in harness.events if e[0] == "send"] == [("send", "do the task")]
        job = d.store.get_job(pid)
        assert job.state == JobState.RUNNING
        (operation,) = d.store.control_operations_for_job(pid)
        assert operation.delivery_phase is ControlDeliveryPhase.DISPATCHED
        assert operation.delivery_result is None
    finally:
        for p in spawned:
            await teardown_backend(d, p)
        await d.aclose()


async def test_refused_preflight_crashes_the_spawn_job(
    native_rig, terminal_provider, tmp_path
) -> None:
    """A send-preflight refusal fires before the send body; the launch still ends loud."""
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    spawned = []
    try:

        async def refuse(participant_id: str) -> None:
            raise BadRequest("preflight refusal: the transcript is unreadable")

        d.controls._gates = replace(d.controls._gates, send_preflight=refuse)
        accepted = await _public_spawn(
            d, key="preflight-refused", prompt="do the task", cwd=tmp_path
        )
        await settle_operations(d)
        pid = accepted["participant_id"]
        spawned.append(pid)

        job = d.store.get_job(pid)
        assert job.state == JobState.CRASHED
        assert "preflight refusal" in (job.result or "")
        assert job.error_code == "bad_request"
        assert d.store.control_operations_for_job(pid) == []
    finally:
        for p in spawned:
            await teardown_backend(d, p)
        await d.aclose()


async def test_refused_structured_spawn_keeps_contract_and_wakes_the_current_waiter(
    native_rig, terminal_provider, tmp_path
) -> None:
    """A refused structured spawn finishes through JobManager, not a bare store write."""
    harness = _SendRefusingHarness()
    d = await native_daemon(native_rig.io, harness, terminal_provider)
    spawned = []
    notified: list[str] = []
    d.jobs.add_terminal_listener(notified.append)
    about_to_send = asyncio.Event()
    release_send = asyncio.Event()
    real_send = d.spawner.controls.send
    captured: dict[str, str] = {}

    async def gated_send(pid, **kwargs):
        captured.setdefault("pid", pid)
        about_to_send.set()
        await release_send.wait()
        return await real_send(pid, **kwargs)

    d.spawner.controls.send = gated_send
    try:
        # The structured contract reaches a spawn only through the private RPC
        # route; the public spawn schema has no response_format field.
        spawn_task = asyncio.create_task(
            private_operations.spawn(
                d,
                {
                    "harness": "native-fixture",
                    "approval": "manual",
                    "cwd": str(tmp_path),
                    "prompt": "do the task",
                    "response_format": {"type": "object"},
                },
            )
        )
        await asyncio.wait_for(about_to_send.wait(), timeout=10)
        pid = captured["pid"]
        # The send stays gated shut, so the job is still RUNNING: one yield
        # parks await_jobs on the job's event, and not-done proves it is
        # waiting on the event itself, not short-circuiting on a terminal job.
        waiter = asyncio.create_task(d.jobs.await_jobs([pid], max_wait=30))
        await asyncio.sleep(0)
        assert not waiter.done()
        release_send.set()
        await spawn_task
        # The parked waiter must wake on the crash itself; wait_for only
        # bounds a hang, not the expected wake.
        (job,) = await asyncio.wait_for(waiter, timeout=5)
        spawned.append(pid)

        assert job.state == JobState.CRASHED
        assert "does not support send" in (job.result or "")
        assert job.error_code == "bad_request"
        # The canonical finish preserves the declared response contract.
        assert job.response_format == '{"type":"object"}'
        assert notified == [pid]
    finally:
        for p in spawned:
            await teardown_backend(d, p)
        await d.aclose()


async def test_parented_public_spawn_keeps_the_parent_as_the_spawn_caller(
    native_rig, terminal_provider, tmp_path
) -> None:
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    spawned = []
    try:
        parent = await _public_spawn(d, key="parent", prompt=None, cwd=tmp_path)
        await settle_operations(d)
        spawned.append(parent["participant_id"])
        child = await _public_spawn(
            d, key="child", prompt="child task", cwd=tmp_path, parent=parent["participant_id"]
        )
        await settle_operations(d)
        pid = child["participant_id"]
        spawned.append(pid)

        job = d.store.get_job(pid)
        assert job.caller_id == parent["participant_id"]
        assert job.actor_client_id == "operator-a"
        assert job.actor_participant_id == parent["participant_id"]
        assert [e for e in native_rig.harness.events if e[0] == "send"] == [("send", "child task")]
    finally:
        for p in spawned:
            await teardown_backend(d, p)
        await d.aclose()
