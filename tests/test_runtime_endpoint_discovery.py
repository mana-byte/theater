"""Stdout endpoint discovery, secret-env resolution, and their persistence.

Real processes announce real endpoints; the core owns the bounds (deadline,
bytes, line length, loopback shape) and the plugin owns only line parsing.
Every failure reaps the just-launched child and fails closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from theater import paths
from theater.daemon.harness_runtime import backend as harness_backend
from theater.daemon.harness_runtime.backend import (
    backend_artifacts_dir,
    launch_detached_backend,
)
from theater.daemon.harness_runtime.errors import BackendLaunchError
from theater.daemon.persistence.repositories.runtime_bindings import (
    ParticipantRuntimeBinding,
)
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    HarnessRuntime,
    LiveChannelDeclaration,
    RuntimeCredentialDeclaration,
    RuntimeEndpointDiscovery,
    RuntimeHost,
    RuntimeLifecyclePhase,
    RuntimeManifest,
    RuntimePlan,
    RuntimePlanningContext,
    RuntimeSessionOrder,
    RuntimeWiring,
)

if TYPE_CHECKING:
    from theater.daemon.persistence.store import Store

from theater.harness.contracts.channels import (
    ChannelBounds,
    ChannelDeclaration,
    ChannelKind,
)

_ANNOUNCE_RE = re.compile(r"^opencode server listening on (\S+)$")


def _parse_announcement(line: str) -> str | None:
    match = _ANNOUNCE_RE.match(line)
    return match.group(1) if match else None


def _announce(port: int) -> str:
    return (
        "import time\n"
        f"print('opencode server listening on http://127.0.0.1:{port}', flush=True)\n"
        "time.sleep(300)\n"
    )


def _plan(argv: list[str], *, max_bytes: int = 4096) -> RuntimePlan:
    return RuntimePlan(
        backend=LaunchPlan(argv=argv),
        endpoint_discovery=RuntimeEndpointDiscovery(
            parser=_parse_announcement, max_bytes=max_bytes
        ),
    )


async def _launch(plan: RuntimePlan, participant: str, cwd: Path):
    return await launch_detached_backend(plan, participant_id=participant, cwd=cwd)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    directory = tmp_path / "work"
    directory.mkdir()
    return directory


def _read_log(participant: str) -> str:
    return (backend_artifacts_dir(participant) / "backend.stdout.log").read_text()


def _log_pid(participant: str) -> int:
    match = re.search(r"^pid (\d+)$", _read_log(participant), re.MULTILINE)
    assert match is not None, "the backend did not log its pid"
    return int(match.group(1))


# ---- contract validation ---------------------------------------------


def test_endpoint_discovery_contract_bounds() -> None:
    for parser in [None, "not-callable"]:
        with pytest.raises(TypeError):
            RuntimeEndpointDiscovery(parser=parser, max_bytes=16)  # type: ignore[arg-type]
    for max_bytes in [0, -1, "64"]:
        with pytest.raises(ValueError):
            RuntimeEndpointDiscovery(
                parser=_parse_announcement,
                max_bytes=max_bytes,  # type: ignore[arg-type]
            )


def test_runtime_plan_requires_exactly_one_endpoint_source() -> None:
    discovery = RuntimeEndpointDiscovery(parser=_parse_announcement, max_bytes=64)
    assert RuntimePlan(backend=LaunchPlan(argv=["a"]), endpoint="unix:///tmp/x.sock").endpoint
    assert RuntimePlan(
        backend=LaunchPlan(argv=["a"]), endpoint_discovery=discovery
    ).endpoint_discovery
    with pytest.raises(ValueError):
        RuntimePlan(backend=LaunchPlan(argv=["a"]))
    with pytest.raises(ValueError):
        RuntimePlan(
            backend=LaunchPlan(argv=["a"]),
            endpoint="unix:///x",
            endpoint_discovery=discovery,
        )


def _manifest(**overrides) -> RuntimeManifest:
    """A minimal valid detached manifest; ``overrides`` replace any field."""

    async def factory(context) -> HarnessRuntime:
        raise AssertionError("never launched")

    channel_decl = ChannelDeclaration(id="x", kind=ChannelKind.LIVE, bounds=ChannelBounds())
    values: dict = {
        "probe": None,
        "plan": None,
        "factory": factory,
        "channel": LiveChannelDeclaration(channel=channel_decl),
        "host": RuntimeHost.FRONTEND,
        "frontend_installer": lambda context: None,
    }
    values.update(overrides)
    return RuntimeManifest(**values)


def test_discovery_and_credential_are_detached_manifest_only() -> None:
    discovery = RuntimeEndpointDiscovery(parser=_parse_announcement, max_bytes=64)
    credential = RuntimeCredentialDeclaration(
        channel_id="runtime-basic", env=("OPENCODE_SERVER_PASSWORD",)
    )
    with pytest.raises(ValueError, match="detached-backend only"):
        _manifest(endpoint_discovery=discovery)
    with pytest.raises(ValueError, match="detached-backend only"):
        _manifest(runtime_credential=credential)
    manifest = _manifest(
        host=RuntimeHost.DETACHED_BACKEND,
        plan=lambda context: RuntimePlan(
            backend=LaunchPlan(argv=["a"]), endpoint_discovery=discovery
        ),
        endpoint_discovery=discovery,
        runtime_credential=credential,
        session_order=RuntimeSessionOrder.SESSION_FIRST,
    )
    assert manifest.session_order is RuntimeSessionOrder.SESSION_FIRST


def test_planning_context_accepts_null_endpoint_and_token_file() -> None:
    context = RuntimePlanningContext(participant_id="p", cwd="/tmp", endpoint=None)
    assert context.endpoint is None and context.token_file is None
    with_token = RuntimePlanningContext(
        participant_id="p", cwd="/tmp", endpoint=None, token_file=Path("/tmp/tok")
    )
    assert with_token.token_file == Path("/tmp/tok")
    with pytest.raises(TypeError):
        RuntimePlanningContext(
            participant_id="p",
            cwd="/tmp",
            endpoint=None,
            token_file="not-a-path",  # type: ignore[arg-type]
        )


# ---- endpoint shape --------------------------------------------------


def test_validate_discovered_endpoint_accepts_only_bare_loopback_http() -> None:
    for accepted in (
        "http://127.0.0.1:4096",
        "http://[::1]:4096",
        "http://127.0.0.1:1/",
    ):
        assert harness_backend.validate_discovered_endpoint(accepted) == accepted
    for rejected in (
        "http://localhost:4096",
        "https://127.0.0.1:1",
        "http://0.0.0.0:1",
        "http://example.com:1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:70000",
        "http://user:pw@127.0.0.1:1",
        "http://127.0.0.1:1/path?q=1",
        "http://127.0.0.1:1#f",
        "not a url",
    ):
        with pytest.raises(BackendLaunchError):
            harness_backend.validate_discovered_endpoint(rejected)


# ---- real-process discovery ------------------------------------------


async def test_launch_discovers_only_this_generation_stdout(workdir: Path) -> None:
    participant = "d00000000001"
    # A previous generation's announcement must never be rediscovered.
    old_log = backend_artifacts_dir(participant) / "backend.stdout.log"
    paths.ensure_private_file(old_log)
    old_log.write_text("opencode server listening on http://127.0.0.1:1\n")
    backend = await _launch(_plan([sys.executable, "-c", _announce(4096)]), participant, workdir)
    try:
        assert backend.endpoint == "http://127.0.0.1:4096"
    finally:
        await backend.terminate()


async def test_discovery_reaps_child_when_backend_exits_silently(workdir: Path) -> None:
    participant = "d00000000002"
    snippet = "import os, sys\nprint('pid', os.getpid(), flush=True)\nsys.exit(3)\n"
    with pytest.raises(BackendLaunchError, match="exited with code 3"):
        await _launch(_plan([sys.executable, "-c", snippet]), participant, workdir)
    assert not harness_backend.pid_alive(_log_pid(participant)), "child must be reaped"


async def test_post_launch_identity_failure_reaps_child(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = "d00000000009"
    snippet = "import os, time\nprint('pid', os.getpid(), flush=True)\ntime.sleep(300)\n"
    seen: list[int] = []

    def fail_identity(pid: int):
        seen.append(pid)
        raise RuntimeError(f"identity failed for {pid}")

    monkeypatch.setattr(harness_backend, "capture_process_identity", fail_identity)
    with pytest.raises(RuntimeError, match="identity failed"):
        await _launch(
            RuntimePlan(
                backend=LaunchPlan(argv=[sys.executable, "-c", snippet]),
                endpoint="unix:///tmp/thtr-identity.sock",
            ),
            participant,
            workdir,
        )
    assert len(seen) == 1
    assert not harness_backend.pid_alive(seen[0]), "child must be reaped"


async def test_discovery_rejects_conflicting_endpoints(workdir: Path) -> None:
    participant = "d00000000003"
    snippet = (
        "import os, time\n"
        "print('pid', os.getpid(), flush=True)\n"
        "print('opencode server listening on http://127.0.0.1:1', flush=True)\n"
        "print('opencode server listening on http://127.0.0.1:2', flush=True)\n"
        "time.sleep(300)\n"
    )
    with pytest.raises(BackendLaunchError, match="conflicting endpoints"):
        await _launch(_plan([sys.executable, "-c", snippet], max_bytes=1024), participant, workdir)
    assert not harness_backend.pid_alive(_log_pid(participant)), "child must be reaped"


async def test_discovery_rejects_a_conflict_after_the_first_read(workdir: Path) -> None:
    participant = "d00000000007"
    snippet = (
        "import time\n"
        "print('opencode server listening on http://127.0.0.1:1', flush=True)\n"
        "time.sleep(0.1)\n"
        "print('opencode server listening on http://127.0.0.1:2', flush=True)\n"
        "time.sleep(300)\n"
    )
    with pytest.raises(BackendLaunchError, match="conflicting endpoints"):
        await _launch(_plan([sys.executable, "-c", snippet]), participant, workdir)


async def test_discovery_advances_past_consumed_lines(workdir: Path) -> None:
    participant = "d00000000008"
    snippet = (
        "import time\n"
        "print('first complete noise line', flush=True)\n"
        "time.sleep(0.1)\n"
        "print('opencode server listening on http://127.0.0.1:4097', flush=True)\n"
        "time.sleep(300)\n"
    )
    backend = await _launch(_plan([sys.executable, "-c", snippet]), participant, workdir)
    try:
        assert backend.endpoint == "http://127.0.0.1:4097"
    finally:
        await backend.terminate()


async def test_discovery_enforces_the_plugin_byte_cap(workdir: Path) -> None:
    participant = "d00000000004"
    snippet = (
        "import time\n"
        "for _ in range(20):\n"
        "    print('noise line with some bytes', flush=True)\n"
        "time.sleep(300)\n"
    )
    with pytest.raises(BackendLaunchError, match="wrote more"):
        await _launch(_plan([sys.executable, "-c", snippet], max_bytes=64), participant, workdir)


async def test_discovery_deadline_fails_closed(workdir: Path, monkeypatch) -> None:
    participant = "d00000000005"
    monkeypatch.setattr(harness_backend, "RUNTIME_ENDPOINT_DISCOVERY_DEADLINE_SECONDS", 0.2)
    snippet = "import os, time\nprint('pid', os.getpid(), flush=True)\ntime.sleep(300)\n"
    with pytest.raises(BackendLaunchError, match="did not announce its endpoint"):
        await _launch(
            _plan([sys.executable, "-c", snippet]),
            participant,
            workdir,
        )
    assert not harness_backend.pid_alive(_log_pid(participant)), "child must be reaped"


async def test_discovery_rejects_oversized_lines(workdir: Path) -> None:
    participant = "d00000000006"
    snippet = "import time\nprint('x' * 5000, flush=True)\ntime.sleep(300)\n"
    with pytest.raises(BackendLaunchError, match="oversized stdout line"):
        await _launch(_plan([sys.executable, "-c", snippet], max_bytes=8192), participant, workdir)


# ---- secret env ------------------------------------------------------


async def test_secret_env_reaches_the_backend_without_leaking(workdir: Path) -> None:
    participant = "s00000000001"
    token_path = paths.participant_dir(participant) / "launch" / "runtime.token"
    paths.ensure_private_file(token_path)
    token_path.write_text("sekrit-token")
    expected_digest = hashlib.sha256(b"sekrit-token").hexdigest()
    snippet = (
        "import hashlib, os, time\n"
        f"print(hashlib.sha256(os.environ['OC_PW'].encode()).hexdigest() == "
        f"'{expected_digest}', flush=True)\n"
        "time.sleep(300)\n"
    )
    plan = RuntimePlan(
        backend=LaunchPlan(
            argv=[sys.executable, "-c", snippet],
            secret_env={"OC_PW": token_path},
        ),
        endpoint="unix:///tmp/thtr-secret.sock",
    )
    assert "sekrit-token" not in repr(plan), "token bytes must never appear in a repr"
    assert all("sekrit-token" not in part for part in plan.backend.argv)
    backend = await _launch(plan, participant, workdir)
    try:
        for _ in range(200):
            if "True" in _read_log(participant):
                break
            await asyncio.sleep(0.05)
        log = _read_log(participant)
        assert "True" in log, "secret must reach the backend env"
        assert "sekrit-token" not in log
    finally:
        await backend.terminate()


async def test_secret_env_refuses_insecure_token_files(workdir: Path) -> None:
    participant = "s00000000002"
    permissive = paths.participant_dir(participant) / "launch" / "permissive.token"
    paths.ensure_private_file(permissive)
    permissive.write_text("sekrit")
    permissive.chmod(0o644)
    plan = RuntimePlan(
        backend=LaunchPlan(argv=["true"], secret_env={"OC_PW": permissive}),
        endpoint="unix:///tmp/thtr-permissive.sock",
    )
    with pytest.raises(BackendLaunchError, match="too permissive"):
        await _launch(plan, participant, workdir)


# ---- persistence -----------------------------------------------------


def test_record_discovered_endpoint_is_generation_guarded(store: Store) -> None:
    store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id="p1",
            harness="opencode",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.STARTED,
            endpoint=None,
            created_at=100.0,
            updated_at=100.0,
        )
    )
    assert store.record_runtime_endpoint(
        "p1", backend_generation=1, endpoint="http://127.0.0.1:4096", updated_at=101.0
    )
    assert store.get_runtime_binding("p1") is not None
    binding = store.get_runtime_binding("p1")
    assert binding is not None and binding.endpoint == "http://127.0.0.1:4096"
    assert not store.record_runtime_endpoint(
        "p1", backend_generation=2, endpoint="http://127.0.0.1:1", updated_at=102.0
    )
    binding = store.get_runtime_binding("p1")
    assert binding is not None and binding.endpoint == "http://127.0.0.1:4096"
