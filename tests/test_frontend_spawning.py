"""Passive frontend spawn fallback coverage."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from theater import paths
from theater.daemon.server import Daemon
from theater.daemon.spawning.models import SpawnRequest
from theater.harness import HARNESSES, Harness
from theater.harness.builtin.plugins.opencode.runtime import opencode_frontend_runtime_factory
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.harness import LaunchParameterSupport
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlTransport,
    LiveChannelDeclaration,
    RuntimeCapability,
    RuntimeCompatibility,
    RuntimeFrontendOverlay,
    RuntimeHost,
    RuntimeLifecyclePhase,
    RuntimeManifest,
)
from theater.harness.observation import TranscriptObserver
from theater.models import Status


class _Observer(TranscriptObserver):
    has_transcript = False

    def find_transcript(self, *, cwd, session_id=None, after=None):
        del cwd, session_id, after

    def session_id(self, transcript):
        del transcript

    def parse(self, line, index, *, clip_text=True):
        del line, index, clip_text
        return []

    def is_idle_screen(self, capture):
        del capture
        return False

    def validate_transcript_receipt(self, *, payload, cwd, expected_session_id):
        del payload, cwd, expected_session_id


class _FrontendHarness(Harness):
    name = "frontend-test"
    binary = sys.executable
    icon = "F"
    launch_parameter_support = LaunchParameterSupport(model=True)

    def __init__(self, *, fail_install: bool = False) -> None:
        self.observer = _Observer()
        self.fail_install = fail_install
        self.runtime = RuntimeManifest(
            probe=lambda context: RuntimeCompatibility(
                supported=True,
                policy="frontend-test-1",
                native_version="1.18.29",
            ),
            plan=None,
            factory=opencode_frontend_runtime_factory,
            channel=LiveChannelDeclaration(
                channel=ChannelDeclaration(id="frontend-live", kind=ChannelKind.LIVE),
                drives_job_completion=False,
            ),
            host=RuntimeHost.FRONTEND,
            frontend_installer=self._install,
            legacy_fallback=frozenset(
                {
                    RuntimeCapability.SEND,
                    RuntimeCapability.QUEUE_FOLLOWUP,
                    RuntimeCapability.INTERRUPT,
                }
            ),
        )

    def _install(self, context) -> RuntimeFrontendOverlay:
        if self.fail_install:
            raise RuntimeError("test installer refused")
        path = (
            paths.participant_observation_dir(context.participant_id, self.name) / "frontend.json"
        )
        return RuntimeFrontendOverlay(env={"FRONTEND_TEST": "1"}, files={path: "{}"})

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        mcp_servers=(),
    ) -> LaunchPlan:
        del config_path, approval, model, mcp_servers
        return LaunchPlan(
            argv=[self.binary, "-c", "pass"],
            receipt_token_path=paths.participant_observation_dir(participant_id, self.name)
            / "receipt-token",
        )


async def _daemon(harness: _FrontendHarness) -> Daemon:
    daemon = Daemon(harnesses={})
    HARNESSES[harness.name] = harness
    await daemon.start()
    return daemon


def _request() -> SpawnRequest:
    return SpawnRequest(
        harness="frontend-test",
        prompt="stock prompt",
        cwd="/tmp",
        approval="manual",
    )


async def test_frontend_spawn_keeps_the_stock_plan_and_passive_binding(fake_tmux) -> None:
    fake_tmux.visible_panes.clear()
    daemon = await _daemon(_FrontendHarness())
    try:
        participant = await daemon.spawner.spawn(_request())
        binding = daemon.store.get_runtime_binding(participant.id)
        assert binding is not None
        assert binding.native_version == "1.18.29"
        assert binding.compatibility_policy == "frontend-test-1"
        assert daemon.runtime_manager.get(participant.id) is None
        assert daemon.controls.route_for(participant.id, RuntimeCapability.SEND).is_legacy
        assert fake_tmux.windows[0]["command"] == [sys.executable, "-c", "pass"]
        assert fake_tmux.windows[0]["env"]["FRONTEND_TEST"] == "1"
    finally:
        await daemon.aclose()


async def test_frontend_connection_keeps_an_active_stock_launch_active(fake_tmux) -> None:
    fake_tmux.visible_panes.clear()
    daemon = await _daemon(_FrontendHarness())
    writer = None
    try:
        participant = await daemon.spawner.spawn(_request())
        binding = daemon.store.get_runtime_binding(participant.id)
        assert binding is not None and binding.endpoint is not None
        credential = daemon.store.get_channel_credential(
            participant.id, ChannelKind.LIVE, "frontend-live"
        )
        assert credential is not None
        _reader, writer = await asyncio.open_unix_connection(
            binding.endpoint.removeprefix("unix://")
        )
        writer.write(
            (
                json.dumps(
                    {"type": "hello", "protocol": "theater-frontend-v1", "token": credential.token}
                )
                + "\n"
            ).encode()
        )
        await writer.drain()
        for _ in range(20):
            if daemon.runtime_manager.get(participant.id) is not None:
                break
            await asyncio.sleep(0)
        assert daemon.runtime_manager.get(participant.id) is not None
        binding = daemon.store.get_runtime_binding(participant.id)
        assert binding is not None
        assert binding.lifecycle is RuntimeLifecyclePhase.ACTIVE
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await daemon.aclose()


async def test_frontend_recovery_keeps_a_proven_unsent_legacy_queue(fake_tmux) -> None:
    fake_tmux.visible_panes.clear()
    harness = _FrontendHarness()
    first = await _daemon(harness)
    second = None
    try:
        participant = await first.spawner.spawn(_request())
        first.registry.set_status(participant.id, Status.WORKING)
        queued = await first.controls.queue_followup(
            participant.id,
            caller_id="cli",
            prompt="after restart",
        )
        await first.aclose()
        first = None

        second = Daemon(harnesses={})
        HARNESSES[harness.name] = harness
        await second.start()

        (operation,) = second.store.queued_control_operations(participant.id)
        assert operation.job_handle == queued.handle
        assert operation.transport is ControlTransport.LEGACY_TMUX
        assert operation.delivery_phase is ControlDeliveryPhase.QUEUED
        assert second.store.get_job(queued.handle).state == "running"
    finally:
        if first is not None:
            await first.aclose()
        if second is not None:
            await second.aclose()


async def test_frontend_install_failure_keeps_the_ordinary_launch(fake_tmux) -> None:
    fake_tmux.visible_panes.clear()
    daemon = await _daemon(_FrontendHarness(fail_install=True))
    try:
        participant = await daemon.spawner.spawn(_request())
        assert daemon.store.get_runtime_binding(participant.id) is None
        assert fake_tmux.windows[0]["command"] == [sys.executable, "-c", "pass"]
        assert "FRONTEND_TEST" not in fake_tmux.windows[0]["env"]
    finally:
        await daemon.aclose()


@pytest.mark.parametrize("invalid_result", [False, True])
async def test_optional_probe_failure_keeps_the_ordinary_launch(fake_tmux, invalid_result):
    fake_tmux.visible_panes.clear()
    harness = _FrontendHarness()

    def broken_probe(_context):
        if invalid_result:
            return {}
        raise RuntimeError("probe unavailable")

    harness.runtime = replace(harness.runtime, probe=broken_probe)
    daemon = await _daemon(harness)
    try:
        participant = await daemon.spawner.spawn(_request())
        assert daemon.store.get_runtime_binding(participant.id) is None
        assert fake_tmux.windows[0]["command"] == [sys.executable, "-c", "pass"]
        assert "FRONTEND_TEST" not in fake_tmux.windows[0]["env"]
    finally:
        await daemon.aclose()


async def test_listener_start_failure_falls_back_before_the_pane_launch(
    fake_tmux, monkeypatch
) -> None:
    fake_tmux.visible_panes.clear()
    daemon = await _daemon(_FrontendHarness())

    async def refuse(**kwargs) -> None:
        del kwargs
        raise OSError("listener unavailable")

    monkeypatch.setattr(daemon.frontend_runtime_host, "start", refuse)
    try:
        participant = await daemon.spawner.spawn(_request())
        assert daemon.store.get_runtime_binding(participant.id) is None
        assert len(fake_tmux.windows) == 1
        assert "FRONTEND_TEST" not in fake_tmux.windows[0]["env"]
    finally:
        await daemon.aclose()
