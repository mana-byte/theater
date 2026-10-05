"""OpenCode dialect verification and routing safety: version probes, binary pinning,
marker-based reconnect dialect selection, and event-loop isolation of pure callbacks."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from theater.daemon.runtime import wiring as wiring_mod
from theater.daemon.spawning import planning
from theater.daemon.spawning.models import NativeSpawnSelection, SpawnRequest
from theater.daemon.spawning.native import _prepare_backend_plan, select_native_wiring
from theater.daemon.spawning.service import Spawner
from theater.harness import HARNESSES, Harness, LaunchPlan
from theater.harness import get as get_harness
from theater.harness.base import ResumeLaunchOverlay
from theater.harness.builtin.plugins.opencode import dialect
from theater.harness.builtin.plugins.opencode.dialect import (
    OpenCodeDialect,
    installed_dialect,
    installed_version,
    is_v2_participant,
    resolve_binary,
    v2_lineage_marker,
)
from theater.harness.builtin.plugins.opencode.server_plan import plan_opencode_server
from theater.harness.builtin.plugins.opencode.server_runtime import (
    OpenCodeServerRuntime,
    opencode_server_runtime_factory,
)
from theater.harness.builtin.plugins.opencode.server_runtime_v2 import OpenCodeServerV2Runtime
from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.harness import LaunchParameterSupport
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    RuntimeCompatibility,
    RuntimeContext,
    RuntimeIO,
    RuntimeManifest,
    RuntimePlan,
    RuntimePlanningContext,
)
from theater.harness.observation import TranscriptObserver
from theater.models import BadRequest, Status


class _IO(RuntimeIO):
    async def connect(self, endpoint: str, *, timeout: float) -> Any:
        del endpoint, timeout
        raise AssertionError("the server runtime never opens a frontend connection")


@pytest.fixture(autouse=True)
def _fresh_version_cache(monkeypatch):
    monkeypatch.setattr(dialect, "_versions", {})


def _context(tmp_path: Path, participant_id: str = "h00000000001") -> RuntimeContext:
    token = tmp_path / "runtime.token"
    token.write_text("test-password")
    token.chmod(0o600)
    return RuntimeContext(
        participant_id=participant_id,
        cwd="/tmp",
        io=_IO(),
        backend_generation=1,
        endpoint="http://127.0.0.1:1",
        token_file=token,
    )


def test_resolve_binary_pins_the_absolute_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\necho 1.18.29\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_binary("opencode") == str(binary.resolve())
    with pytest.raises(BadRequest, match="not on PATH"):
        resolve_binary("definitely-not-opencode")


def test_unverified_version_refuses_before_any_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\necho not-a-version\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv(dialect.VERSION_ENV, raising=False)

    with pytest.raises(BadRequest, match=dialect.VERSION_ENV):
        installed_dialect("opencode")
    # The daemon's planning entry refuses the same way, before any backend or
    # participant work could touch a database.
    req = SpawnRequest(harness="opencode", prompt="", cwd="/tmp", approval="manual")
    participant = SimpleNamespace(id="h00000000001", harness="opencode")
    with pytest.raises(BadRequest, match=dialect.VERSION_ENV):
        asyncio.run(planning.build_plan(req, participant, None, registry=None))


def test_an_explicit_pin_supplies_the_release_without_a_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no opencode at all
    monkeypatch.setenv(dialect.VERSION_ENV, "2.0.18")
    assert installed_dialect("opencode") is OpenCodeDialect.V2


def _counting_binary(directory: Path, body: str) -> tuple[Path, Path]:
    counter = directory / "opencode.runs"
    counter.write_text("")
    binary = directory / "opencode"
    binary.write_text(f'#!/bin/sh\necho run >> "{counter}"\n{body}\n')
    binary.chmod(0o755)
    return binary, counter


def test_a_failed_probe_caches_within_the_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _binary, counter = _counting_binary(tmp_path, "echo not-a-version")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv(dialect.VERSION_ENV, raising=False)

    assert installed_version("opencode") is None
    assert installed_version("opencode") is None
    assert len(counter.read_text().splitlines()) == 1  # the failure cached


def test_a_failed_probe_expires_and_recovers_on_an_unchanged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, counter = _counting_binary(tmp_path, "echo not-a-version")
    identity = binary.stat()
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv(dialect.VERSION_ENV, raising=False)
    monkeypatch.setattr(dialect, "_FAILURE_CACHE_SECONDS", 0.05)

    assert installed_version("opencode") is None
    assert len(counter.read_text().splitlines()) == 1

    # Repair the CLI with the same size and mtime: only the failure's expiry
    # (not a file-identity change) can drive the re-probe.
    repaired = f'#!/bin/sh\necho run >> "{counter}"\necho 1.18.29\n'
    padding = identity.st_size - len(repaired.encode())
    assert padding >= 2
    repaired += "#" + "x" * (padding - 2) + "\n"
    binary.write_text(repaired)
    os.utime(binary, ns=(identity.st_atime_ns, identity.st_mtime_ns))
    assert binary.stat().st_size == identity.st_size

    time.sleep(0.1)  # past the patched backoff, far under the 20s probe timeout
    assert installed_version("opencode") == (1, 18, 29)
    assert len(counter.read_text().splitlines()) == 2


def test_the_factory_selects_v1_for_an_unmarked_participant_despite_an_installed_2x(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(dialect.VERSION_ENV, "2.0.18")
    runtime = opencode_server_runtime_factory(_context(tmp_path))
    assert isinstance(runtime, OpenCodeServerRuntime)


def test_the_factory_selects_v2_from_the_marker_despite_an_installed_1x(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(dialect.VERSION_ENV, "1.18.29")
    marker, payload = v2_lineage_marker("h00000000001")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(payload)
    runtime = opencode_server_runtime_factory(_context(tmp_path))
    assert isinstance(runtime, OpenCodeServerV2Runtime)


def test_the_factory_selects_v2_when_the_cli_is_missing_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker, payload = v2_lineage_marker("h00000000001")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(payload)
    monkeypatch.setenv("PATH", str(tmp_path))  # no opencode on PATH
    monkeypatch.delenv(dialect.VERSION_ENV, raising=False)
    runtime = opencode_server_runtime_factory(_context(tmp_path))
    assert isinstance(runtime, OpenCodeServerV2Runtime)


def test_the_v2_marker_is_written_with_the_backend_plan_before_the_factory_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available_harness_binaries
) -> None:
    monkeypatch.setenv(dialect.VERSION_ENV, "2.0.18")
    plan = plan_opencode_server(
        RuntimePlanningContext(
            participant_id="h00000000001",
            cwd=str(tmp_path),
            token_file=tmp_path / "runtime.token",
            approval="manual",
        )
    )
    assert not is_v2_participant("h00000000001")
    planning.write_plan_files(plan.backend)
    # The launch files exist before the runtime factory is invoked, so a reconnect
    # always finds the dialect the running backend was launched with.
    assert is_v2_participant("h00000000001")
    built = opencode_server_runtime_factory(_context(tmp_path))
    assert isinstance(built, OpenCodeServerV2Runtime)


def _stub_manifest(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    def probe(context) -> RuntimeCompatibility:
        captured["probe"] = context
        return RuntimeCompatibility(supported=True, policy="fake-verified", native_version="1.0.0")

    def plan(context) -> Any:
        raise AssertionError("the probe context test never plans a backend")

    manifest = RuntimeManifest(
        probe=probe,
        plan=plan,
        factory=lambda context: None,
        channel=LiveChannelDeclaration(
            channel=ChannelDeclaration(
                id="live",
                kind=ChannelKind.LIVE,
                capabilities=(ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),),
            ),
        ),
    )
    monkeypatch.setattr(wiring_mod, "runtime_manifest_of", lambda _harness: manifest)


async def test_select_native_wiring_passes_binary_and_resume_into_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _stub_manifest(monkeypatch, captured)
    predecessor = SimpleNamespace(id="h00000000002")
    spawner = SimpleNamespace(
        registry=SimpleNamespace(
            store=SimpleNamespace(
                get_runtime_binding=lambda pid: SimpleNamespace(native_session_id="parent-sess")
            )
        )
    )
    req = SpawnRequest(harness="opencode", prompt="", cwd="/tmp", approval="manual")
    participant = SimpleNamespace(id="h00000000001", cwd="/tmp")

    selection = await select_native_wiring(
        spawner, req, get_harness("opencode"), participant, predecessor
    )
    assert selection is not None
    assert captured["probe"].resume is True
    assert captured["probe"].binary == "opencode"
    assert selection.fork_parent_session == "parent-sess"

    selection = await select_native_wiring(spawner, req, get_harness("opencode"), participant, None)
    assert selection is not None
    assert captured["probe"].resume is False
    assert selection.fork_parent_session is None


async def _await_condition(condition, timeout: float = 10.0) -> None:
    """Poll on the event loop; a loop blocked by the callback under test hangs here."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true; the event loop was blocked")
        await asyncio.sleep(0.005)


async def test_the_event_loop_stays_live_during_a_blocked_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry
) -> None:
    # Resolve the blocker before PATH shrinks to the fake binary's directory.
    sleep = shutil.which("sleep")
    assert sleep is not None
    started, release = tmp_path / "started", tmp_path / "release"
    binary = tmp_path / "opencode"
    binary.write_text(
        f"#!/bin/sh\n"
        f": > {started}\n"
        f"while [ ! -e {release} ]; do {sleep} 0.01; done\n"
        f'echo "1.18.29"\n'
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv(dialect.VERSION_ENV, raising=False)

    req = SpawnRequest(harness="opencode", prompt="", cwd="/tmp", approval="yolo")
    participant = SimpleNamespace(id="h00000000001", harness="opencode")
    plan_task = asyncio.create_task(planning.build_plan(req, participant, None, registry=None))
    await _await_condition(started.exists)
    # The probe is genuinely blocked now: store work on the loop still completes.
    registry.store.get_participant("h00000000001")
    assert not plan_task.done()
    release.write_text("go")
    plan = await asyncio.wait_for(plan_task, timeout=10)
    assert plan.argv == [str(binary.resolve()), "--auto"]


class _MinimalHarness(Harness):
    """A harness with no MCP overlay, so backend planning stays file-free."""

    name = "routing-safety-minimal"
    binary = "routing-safety-minimal"
    icon = "R"
    launch_parameter_support = LaunchParameterSupport(model=True, resume=True)

    def __init__(self) -> None:
        class _Obs(TranscriptObserver):
            has_transcript = True

            def find_transcript(self, *, cwd, session_id=None, after=None):
                return None

            def session_id(self, transcript):
                return None

            def parse(self, line, index, *, clip_text=True):
                return []

            def is_idle_screen(self, capture):
                return False

        self.observer = _Obs()

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        resume: str | None = None,
    ) -> LaunchPlan:
        return LaunchPlan(argv=[self.binary])


class _BlockingResumeHarness(_MinimalHarness):
    """A resume harness whose overlay callback blocks until the test releases it."""

    name = "routing-safety-resume"
    binary = "routing-safety-resume"

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.overlay_domain: str | None = None

    def resume_launch_overlay(self, *, predecessor, trusted_session_owners):
        self.overlay_domain = predecessor.transcript_domain
        if not self.gate.wait(timeout=30):
            raise AssertionError("the test never released the overlay callback")
        return ResumeLaunchOverlay()


@pytest.fixture
def blocking_resume_harness(monkeypatch):
    harness = _BlockingResumeHarness()
    monkeypatch.setitem(HARNESSES, harness.name, harness)
    return harness


@pytest.fixture
def minimal_harness(monkeypatch):
    harness = _MinimalHarness()
    monkeypatch.setitem(HARNESSES, harness.name, harness)
    return harness


def _trusted_dead_predecessor(registry, harness: str, domain: str | None):
    participant = registry.register(harness=harness, pane=None, cwd="/tmp", session_id="sess-abc")
    participant.session_correlation = "exact"
    participant.transcript_domain = domain
    registry.store.upsert_participant(participant)
    registry.mark_dead(participant.id)
    return participant


def _resume_request() -> SpawnRequest:
    return SpawnRequest(
        harness="routing-safety-resume", prompt="", cwd="/tmp", approval="edits", resume="sess-abc"
    )


async def test_the_event_loop_stays_live_during_a_blocked_resume_overlay(
    registry, blocking_resume_harness, monkeypatch
) -> None:
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_dead_predecessor(registry, "routing-safety-resume", "opencode://old")
    spawner = Spawner(registry)

    reserve_task = asyncio.create_task(spawner.reserve(_resume_request()))
    await _await_condition(lambda: blocking_resume_harness.overlay_domain == "opencode://old")
    # The pure callback is blocked on a worker thread; store work still runs here.
    registry.store.get_participant("h00000000001")
    assert not reserve_task.done()
    blocking_resume_harness.gate.set()
    reservation = await asyncio.wait_for(reserve_task, timeout=10)
    assert reservation.resume_predecessor is not None


async def test_a_resume_overlay_computed_from_stale_identity_is_rejected(
    registry, blocking_resume_harness, monkeypatch
) -> None:
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    predecessor = _trusted_dead_predecessor(registry, "routing-safety-resume", "opencode://old")
    spawner = Spawner(registry)

    reserve_task = asyncio.create_task(spawner.reserve(_resume_request()))
    await _await_condition(lambda: blocking_resume_harness.overlay_domain == "opencode://old")
    # Same participant id, but the session's identity moved while the pure
    # callback was blocked: the stale overlay must not survive the await.
    predecessor.transcript_domain = "opencode://new"
    predecessor.status = Status.DEAD
    registry.store.upsert_participant(predecessor)
    blocking_resume_harness.gate.set()
    with pytest.raises(BadRequest, match="resume identity changed"):
        await asyncio.wait_for(reserve_task, timeout=10)


async def test_the_event_loop_stays_live_during_a_blocked_backend_planner(
    registry, minimal_harness
) -> None:
    gate = threading.Event()
    entered = threading.Event()

    def planner(context: RuntimePlanningContext) -> RuntimePlan:
        entered.set()
        if not gate.wait(timeout=30):
            raise AssertionError("the test never released the backend planner")
        return RuntimePlan(
            backend=LaunchPlan(argv=["routing-safety-backend"]),
            endpoint="unix:///tmp/routing-safety.sock",
        )

    manifest = RuntimeManifest(
        probe=lambda context: RuntimeCompatibility(supported=True, policy="fake-verified"),
        plan=planner,
        factory=lambda context: None,
        channel=LiveChannelDeclaration(
            channel=ChannelDeclaration(
                id="live",
                kind=ChannelKind.LIVE,
                capabilities=(ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),),
            ),
        ),
    )
    native = NativeSpawnSelection(
        runtime=manifest,
        endpoint="unix:///tmp/routing-safety.sock",
        backend_generation=1,
        compatibility=RuntimeCompatibility(supported=True, policy="fake-verified"),
    )
    participant = registry.create_spawned(harness=minimal_harness.name, cwd="/tmp")
    reservation = SimpleNamespace(
        participant=participant,
        req=SpawnRequest(harness=minimal_harness.name, prompt="", cwd="/tmp", approval="manual"),
        child_cwd="/tmp",
        legacy_plan=None,
    )

    plan_task = asyncio.create_task(_prepare_backend_plan(reservation, native, registry.store))
    await _await_condition(entered.is_set)
    registry.store.get_participant(participant.id)
    assert not plan_task.done()
    gate.set()
    plan, token_file = await asyncio.wait_for(plan_task, timeout=10)
    assert token_file is None
    assert plan.backend.argv == ["routing-safety-backend"]
