"""The stock Pi launch and its optional duplex host must compose end to end."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from dataclasses import replace
from pathlib import Path

from tests.test_pi_native_bridge import bridge_snapshot
from theater.daemon.server import Daemon
from theater.daemon.spawning.models import SpawnRequest
from theater.daemon.spawning.provider_launch import ParticipantLaunchService
from theater.harness import HARNESSES
from theater.harness.builtin.plugins.pi.manifest import MANIFEST
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.runtime import (
    DeliveryResult,
    RuntimeCapability,
    RuntimeCompatibility,
    RuntimeWiring,
)
from theater.harness.manifests.compiler import compile_manifest
from theater.models import JobState, Status


def _install(monkeypatch, *, compatible=True):
    runtime = replace(
        MANIFEST.runtime,
        probe=lambda _: RuntimeCompatibility(
            supported=compatible,
            policy="pi-integration",
            native_version="0.84.4",
            reason=None if compatible else "unsupported version",
        ),
    )
    harness = compile_manifest("pi", replace(MANIFEST, binary=sys.executable, runtime=runtime))
    monkeypatch.setitem(HARNESSES, "pi", harness)
    return harness


async def _wait_for(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


async def _spawn(daemon: Daemon, request: SpawnRequest):
    params = {
        "harness": request.harness,
        "cwd": request.cwd,
        "approval": request.approval,
        "resume": request.resume,
    }
    if request.prompt:
        params["prompt"] = request.prompt
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="pi-frontend-test",
        idempotency_key=f"pi-spawn-{len(daemon.registry.list())}",
        params=params,
        launch_prompt=request.prompt,
        launch_wiring=request.wiring,
    )
    operation, timed_out = await daemon.operation_service.wait(str(accepted["operation_id"]))
    assert not timed_out and operation.state == "succeeded"
    return daemon.registry.get(str(accepted["participant_id"]))


class PiPeer:
    def __init__(self, session_id):
        self.session_id = session_id
        self.thinking = "high"
        self.requests = []
        self.revision = 0

    async def connect(self, descriptor):
        self.reader, self.writer = await asyncio.open_unix_connection(
            descriptor["endpoint"].removeprefix("unix://")
        )
        token = Path(descriptor["token_file"]).read_text().strip()
        self.writer.write(
            json.dumps(
                {
                    "type": "hello",
                    "protocol": descriptor["protocol"],
                    "token": token,
                }
            ).encode()
            + b"\n"
        )
        await self.writer.drain()
        self.task = asyncio.create_task(self._respond())

    async def _respond(self):
        while line := await self.reader.readline():
            request = json.loads(line)
            self.requests.append(request)
            self.revision += 1
            if request["method"] == "pi.settings.update":
                assert request["params"]["native_session_id"] == self.session_id
                self.thinking = request["params"]["reasoning_effort"]
            result = bridge_snapshot(
                session_id=self.session_id,
                bridge_epoch=1,
                snapshot_revision=self.revision,
                thinking=self.thinking,
            )
            if request["method"] == "pi.settings.update":
                result.update(status="accepted", operation_id=request["params"]["operation_id"])
            self.writer.write(
                json.dumps(
                    {
                        "type": "response",
                        "id": request["id"],
                        "result": result,
                    }
                ).encode()
                + b"\n"
            )
            await self.writer.drain()

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task


async def test_pi_launch_connect_settings_and_reconnect_preserve_pinned_routes(
    daemon,
    terminal_provider,
    monkeypatch,
    tmp_path,
):
    harness = _install(monkeypatch)
    terminal_provider.terminals.clear()
    participant = await _spawn(
        daemon,
        SpawnRequest(
            harness="pi",
            prompt="",
            cwd=str(tmp_path),
            approval="yolo",
            wiring=RuntimeWiring.NATIVE,
        ),
    )
    launch = terminal_provider.creations[-1]
    descriptor = json.loads(Path(launch["env"]["THEATER_PI_FRONTEND_CONFIG"]).read_text())
    assert "token" not in descriptor
    assert "--extension" in launch["command"] and "--session-dir" in launch["command"]
    assert "--theater-mcp-config" in launch["command"]
    assert daemon.store.get_receipt_token(participant.id) is None
    credential = daemon.store.get_channel_credential(
        participant.id,
        ChannelKind.LIVE,
        "pi-frontend-live",
    )
    assert credential is not None
    assert Path(credential.token_path).stat().st_mode & 0o777 == 0o600

    peer = PiPeer(participant.session_id)
    await peer.connect(descriptor)
    try:
        await _wait_for(lambda: daemon.runtime_manager.get(participant.id) is not None)
        runtime = daemon.runtime_manager.get(participant.id)
        receipt = await runtime.update_settings(operation_id="thinking-1", reasoning_effort="low")
        assert receipt.result is DeliveryResult.ACCEPTED
        assert (await runtime.snapshot()).settings.reasoning_effort == "low"
        assert sum(r["method"] == "pi.settings.update" for r in peer.requests) == 1

        # A manifest reload cannot change this participant's spawn-pinned routes.
        assert daemon.controls.route_for(participant.id, RuntimeCapability.INTERRUPT).is_native
        harness.runtime = replace(
            harness.runtime,
            legacy_fallback=frozenset({RuntimeCapability.INTERRUPT}),
        )
        for capability in (
            RuntimeCapability.SEND,
            RuntimeCapability.QUEUE_FOLLOWUP,
            RuntimeCapability.INTERRUPT,
        ):
            assert daemon.controls.route_for(participant.id, capability).is_native
    finally:
        await peer.close()
    await _wait_for(lambda: daemon.runtime_manager.get(participant.id) is None)
    # The spawn-pinned route stays native; delivery itself fails closed
    # until a live runtime re-registers.
    assert daemon.controls.route_for(participant.id, RuntimeCapability.SEND).is_native
    assert daemon.controls.route_for(participant.id, RuntimeCapability.INTERRUPT).is_native
    replacement = PiPeer(participant.session_id)
    await replacement.connect(descriptor)
    try:
        await _wait_for(lambda: daemon.runtime_manager.get(participant.id) is not None)
        assert daemon.runtime_manager.get(participant.id) is not runtime
        assert (
            await daemon.runtime_manager.get(participant.id).snapshot()
        ).settings.reasoning_effort == "high"
        assert all(r["method"] == "pi.snapshot" for r in replacement.requests)
    finally:
        await replacement.close()


async def test_explicit_native_preference_falls_back_on_unsupported_pi(
    daemon,
    terminal_provider,
    monkeypatch,
    tmp_path,
):
    _install(monkeypatch, compatible=False)
    participant = await _spawn(
        daemon,
        SpawnRequest(
            harness="pi",
            prompt="",
            cwd=str(tmp_path),
            approval="yolo",
            wiring=RuntimeWiring.NATIVE,
        ),
    )
    assert daemon.store.get_runtime_binding(participant.id) is None
    assert "THEATER_PI_FRONTEND_CONFIG" not in terminal_provider.creations[-1]["env"]
    assert daemon.controls.route_for(participant.id, RuntimeCapability.SEND).is_provider


async def test_pi_daemon_restart_restores_credentials_and_fails_native_followups(
    terminal_provider,
    monkeypatch,
    tmp_path,
):
    terminal_provider.terminals.clear()
    first = Daemon(harnesses={})
    second = None
    peer = None
    replacement = None
    await first.start()
    assert first is not None
    # ``Daemon.__init__`` rebuilds the global harness registry from the
    # shipped plugin directories, so the fake must be installed after each
    # Daemon construction.  Installed before, ``install()`` wipes this entry
    # and the spawn resolves the shipped manifest, whose probe runs the real
    # ``pi --version``: native wiring then depends on a compatible pi being
    # installed, and CI machines without one select a legacy launch instead.
    _install(monkeypatch)
    try:
        participant = await _spawn(
            first,
            SpawnRequest(
                harness="pi",
                prompt="",
                cwd=str(tmp_path),
                approval="yolo",
            ),
        )
        descriptor = json.loads(
            Path(
                terminal_provider.creations[-1]["env"]["THEATER_PI_FRONTEND_CONFIG"],
            ).read_text()
        )
        peer = PiPeer(participant.session_id)
        await peer.connect(descriptor)
        await _wait_for(lambda: first.runtime_manager.get(participant.id) is not None)
        first.registry.set_status(participant.id, Status.WORKING)
        queued = await first.controls.queue_followup(
            participant.id,
            caller_id="cli",
            prompt="after restart",
        )
        await first.aclose()
        first = None
        await peer.close()
        peer = None

        second = Daemon(harnesses={})
        await second.start()
        assert second is not None
        _install(monkeypatch)
        replacement = PiPeer(participant.session_id)
        await replacement.connect(descriptor)
        await _wait_for(lambda: second.runtime_manager.get(participant.id) is not None)
        # Native queued followups are never replayed across a restart: the
        # reconcile fails them explicitly instead of guessing delivery.
        await _wait_for(lambda: second.store.get_job(queued.handle).state == JobState.CRASHED)
        assert second.store.get_job(queued.handle).error_code == "daemon_restarted"
        assert second.controls.route_for(participant.id, RuntimeCapability.SEND).is_native
        assert len(terminal_provider.creations) == 1
        assert all(request["method"] == "pi.snapshot" for request in replacement.requests)
    finally:
        if peer is not None:
            await peer.close()
        if replacement is not None:
            await replacement.close()
        if first is not None:
            await first.aclose()
        if second is not None:
            await second.aclose()


async def test_failed_pi_live_registration_closes_only_the_optional_runtime(
    daemon,
    terminal_provider,
    monkeypatch,
    tmp_path,
):
    _install(monkeypatch)
    participant = await _spawn(
        daemon,
        SpawnRequest(
            harness="pi",
            prompt="",
            cwd=str(tmp_path),
            approval="yolo",
        ),
    )
    descriptor = json.loads(
        Path(
            terminal_provider.creations[-1]["env"]["THEATER_PI_FRONTEND_CONFIG"],
        ).read_text()
    )
    attempted = asyncio.Event()

    def fail_registration(_registration):
        attempted.set()
        raise RuntimeError("injected observer registration failure")

    monkeypatch.setattr(daemon.observer.live, "register", fail_registration)
    peer = PiPeer(participant.session_id)
    await peer.connect(descriptor)
    try:
        await asyncio.wait_for(attempted.wait(), timeout=2)
        await _wait_for(lambda: daemon.runtime_manager.get(participant.id) is None)
        assert daemon.observer.live.registration_for(participant.id) is None
        assert daemon.controls.route_for(participant.id, RuntimeCapability.SEND).is_native
        assert daemon.registry.get(participant.id).tmux_pane == participant.tmux_pane
    finally:
        await peer.close()
