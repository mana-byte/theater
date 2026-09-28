"""OpenCode dialect verification and routing safety: version probes, binary pinning,
marker-based reconnect dialect selection, and event-loop isolation of pure callbacks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from theater.daemon.runtime import wiring as wiring_mod
from theater.daemon.spawning import planning
from theater.daemon.spawning.models import SpawnRequest
from theater.daemon.spawning.native import select_native_wiring
from theater.harness import get as get_harness
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
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    RuntimeCompatibility,
    RuntimeContext,
    RuntimeIO,
    RuntimeManifest,
    RuntimePlanningContext,
)
from theater.models import BadRequest


class _IO(RuntimeIO):
    async def connect(self, endpoint: str, *, timeout: float) -> Any:
        del endpoint, timeout
        raise AssertionError("the server runtime never opens a frontend connection")


@pytest.fixture(autouse=True)
def _fresh_version_cache(monkeypatch):
    monkeypatch.setattr(dialect, "_versions", {})


def _write_fake_binary(directory: Path, output: str, *, delay: float = 0.0) -> Path:
    script = "#!/bin/sh\n"
    if delay:
        script += f"sleep {delay}\n"
    script += f'echo "{output}"\n'
    binary = directory / "opencode"
    binary.write_text(script)
    binary.chmod(0o755)
    return binary


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
    binary = _write_fake_binary(tmp_path, "1.18.29")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_binary("opencode") == str(binary.resolve())
    with pytest.raises(BadRequest, match="not on PATH"):
        resolve_binary("definitely-not-opencode")


def test_unverified_version_refuses_before_any_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_binary(tmp_path, "not-a-version")
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


def test_a_failed_probe_caches_and_a_repaired_binary_reprobes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = tmp_path / "opencode.runs"
    binary = tmp_path / "opencode"
    binary.write_text('#!/bin/sh\necho run >> "$0.runs"\necho not-a-version\n')
    binary.chmod(0o755)
    counter.write_text("")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv(dialect.VERSION_ENV, raising=False)

    assert installed_version("opencode") is None
    assert installed_version("opencode") is None
    assert len(counter.read_text().splitlines()) == 1  # the failure cached

    # Repairing the CLI changes the file identity, so the next probe runs again.
    binary.write_text('#!/bin/sh\necho run >> "$0.runs"\necho 1.18.29\n')
    binary.chmod(0o755)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(dialect.VERSION_ENV, "2.0.18")
    plan = plan_opencode_server(
        RuntimePlanningContext(
            participant_id="h00000000001",
            cwd=str(tmp_path),
            token_file=tmp_path / "runtime.token",
        )
    )
    assert not is_v2_participant("h00000000001")
    planning.write_plan_files(plan.backend)
    # The launch files exist before the runtime factory is invoked, so a reconnect
    # always finds the dialect the running backend was launched with.
    assert is_v2_participant("h00000000001")
    assert isinstance(opencode_server_runtime_factory(_context(tmp_path)), OpenCodeServerV2Runtime)


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


async def test_the_event_loop_stays_live_during_a_slow_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_binary(tmp_path, "1.18.29", delay=0.5)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv(dialect.VERSION_ENV, raising=False)

    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    req = SpawnRequest(harness="opencode", prompt="", cwd="/tmp", approval="manual")
    participant = SimpleNamespace(id="h00000000001", harness="opencode")
    ticker = asyncio.create_task(tick())
    try:
        plan = await planning.build_plan(req, participant, None, registry=None)
    finally:
        ticker.cancel()
    assert plan.argv == ["opencode"]
    # A blocked loop would tick zero times while the probe sleeps half a second.
    assert ticks >= 3
