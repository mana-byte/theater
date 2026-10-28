"""The public spawn's initial prompt on a detached-native harness: once, and loud on refusal."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.support.native_lifecycle import (
    NativeFixtureHarness,
    native_daemon,
    teardown_backend,
)
from tests.support.waiting import settle_operations
from theater.daemon.spawning.provider_launch import ParticipantLaunchService
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    DeliveryResult,
    RuntimeCapability,
    RuntimeSessionOrder,
)
from theater.models import JobState

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
    """A native fixture whose backend write is lost, so the delivery is uncertain."""

    def _manifest(self):
        manifest = super()._manifest()

        def factory(context):
            runtime = manifest.factory(context)

            async def send(*, operation_id, prompt):
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

        job = d.store.get_job(pid)
        assert job.state == JobState.RUNNING
        # One uncertain operation, never settled as a refusal and never replayed.
        (operation,) = d.store.control_operations_for_job(pid)
        assert operation.delivery_result is None
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
