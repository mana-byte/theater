"""OpenCode version and flag probes: qualified windows, failures, cached results."""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.opencode_probe import ProbeResult, patch_probe
from theater.daemon.spawning import planning
from theater.daemon.spawning.models import SpawnRequest
from theater.harness.builtin.plugins.opencode import dialect, server_plan
from theater.harness.builtin.plugins.opencode.dialect import (
    OpenCodeDialect,
    installed_dialect,
    installed_version,
    resolve_binary,
)
from theater.harness.builtin.plugins.opencode.runtime_plan import (
    OPENCODE_SERVER_COMPATIBILITY_POLICY,
    OPENCODE_SERVER_MAX_VERSION,
    OPENCODE_SERVER_MIN_VERSION,
    OPENCODE_TUI_COMPATIBILITY_POLICY,
    OPENCODE_TUI_MAX_VERSION,
    OPENCODE_V1_QUALIFIED_RANGE,
    probe_opencode_compatibility,
    release_text,
)
from theater.harness.builtin.plugins.opencode.server_plan import (
    probe_opencode_server_compatibility,
)
from theater.harness.contracts.runtime import (
    RuntimeProbeContext,
)
from theater.models import BadRequest


@pytest.mark.parametrize("returncodes", [(0, 0), (1, 0), (0, 1)])
def test_probe_requires_both_successful_checks(
    monkeypatch: pytest.MonkeyPatch, returncodes: tuple[int, int]
) -> None:
    patch_probe(monkeypatch, "opencode 1.18.29+c470c79\n", "--port --hostname", returncodes)
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is (returncodes == (0, 0))
    if compatibility.supported:
        assert compatibility.native_version == "1.18.29"


def _patch_offset(version: tuple[int, int, int], offset: int) -> str:
    return release_text((*version[:2], version[2] + offset))


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        (_patch_offset(OPENCODE_SERVER_MIN_VERSION, -1), False),
        (release_text(OPENCODE_SERVER_MIN_VERSION), True),
        (_patch_offset(OPENCODE_SERVER_MAX_VERSION, -1), True),
        (release_text(OPENCODE_SERVER_MAX_VERSION), False),
        (_patch_offset(OPENCODE_SERVER_MAX_VERSION, 1), False),
        (
            release_text((OPENCODE_SERVER_MAX_VERSION[0], OPENCODE_SERVER_MAX_VERSION[1] + 1, 0)),
            False,
        ),
    ],
)
def test_probe_qualifies_exactly_the_1x_window(
    monkeypatch: pytest.MonkeyPatch, version: str, supported: bool
) -> None:
    patch_probe(monkeypatch, version, "--port --hostname")
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is supported
    assert compatibility.policy == OPENCODE_SERVER_COMPATIBILITY_POLICY
    if not supported:
        assert compatibility.reason == (
            "OpenCode release is outside the server-topology compatibility "
            f"range; qualified releases are {OPENCODE_V1_QUALIFIED_RANGE}"
        )


def test_probe_requires_the_serve_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_probe(monkeypatch, "1.18.29", "--model --auto")
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False
    assert "port-0 flags" in (compatibility.reason or "")


@pytest.mark.parametrize(
    ("version", "supported"),
    [("2.0.5", False), ("2.0.6", True), ("2.0.20", True), ("2.1.0", False)],
)
def test_probe_qualifies_exactly_the_2x_window(
    monkeypatch: pytest.MonkeyPatch, version: str, supported: bool
) -> None:
    patch_probe(monkeypatch, f"opencode v{version}\n", "--port --hostname")
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is supported
    if not supported:
        assert "compatibility range" in (compatibility.reason or "")


def test_a_2x_resume_probe_refuses_before_any_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume continues in the lineage database, which the native backend cannot open."""
    patch_probe(monkeypatch, "opencode v2.0.18\n", "--port --hostname")
    refused = probe_opencode_server_compatibility(
        RuntimeProbeContext(binary="opencode", resume=True)
    )
    assert refused.supported is False
    assert "legacy route" in (refused.reason or "")


def test_a_1x_resume_probe_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_probe(monkeypatch, "opencode 1.18.29+c470c79\n", "--port --hostname")
    qualified = probe_opencode_server_compatibility(
        RuntimeProbeContext(binary="opencode", resume=True)
    )
    assert qualified.supported is True
    assert qualified.native_version == "1.18.29"


@pytest.mark.parametrize("failing_check", ["--version", "--help"])
@pytest.mark.parametrize("failure", [OSError("no binary"), subprocess.TimeoutExpired("probe", 1)])
def test_probe_reports_probe_failures_and_joins_both_checks(
    monkeypatch: pytest.MonkeyPatch, failing_check: str, failure: Exception
) -> None:
    started = threading.Barrier(2, timeout=5)
    finished: set[str] = set()

    def run(argv: list[str], **kwargs: object) -> ProbeResult:
        del kwargs
        started.wait()
        try:
            if argv[-1] == failing_check:
                raise failure
            return ProbeResult("1.18.29" if argv[-1] == "--version" else "--port --hostname")
        finally:
            finished.add(argv[-1])

    monkeypatch.setattr(server_plan.subprocess, "run", run)
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False
    assert "probes" in (compatibility.reason or "")
    assert finished == {"--version", "--help"}


def test_probe_accepts_the_declared_release_range(monkeypatch) -> None:
    class _Result:
        def __init__(self, output: str) -> None:
            self.returncode = 0
            self.stdout = output
            self.stderr = ""

    def run(argv, **kwargs):
        del kwargs
        return _Result("1.18.29") if argv[-1] == "--version" else _Result("--model --auto --fork")

    monkeypatch.setattr(
        "theater.harness.builtin.plugins.opencode.runtime_plan.subprocess.run",
        run,
    )

    compatibility = probe_opencode_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is True
    assert compatibility.policy == OPENCODE_TUI_COMPATIBILITY_POLICY


def test_probe_rejects_a_prerelease(monkeypatch) -> None:
    class _Result:
        returncode = 0
        stdout = "1.18.29-beta.1"
        stderr = ""

    monkeypatch.setattr(
        "theater.harness.builtin.plugins.opencode.runtime_plan.subprocess.run",
        lambda *args, **kwargs: _Result(),
    )

    compatibility = probe_opencode_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False


def test_probe_rejects_releases_outside_the_tested_window(monkeypatch) -> None:
    class _Result:
        def __init__(self, output: str) -> None:
            self.returncode = 0
            self.stdout = output
            self.stderr = ""

    def run(argv, **kwargs):
        del kwargs
        version = release_text(OPENCODE_TUI_MAX_VERSION)
        return _Result(version) if argv[-1] == "--version" else _Result("--model --auto --fork")

    monkeypatch.setattr(
        "theater.harness.builtin.plugins.opencode.runtime_plan.subprocess.run",
        run,
    )

    compatibility = probe_opencode_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False
    assert compatibility.policy == OPENCODE_TUI_COMPATIBILITY_POLICY
    assert OPENCODE_V1_QUALIFIED_RANGE in (compatibility.reason or "")


@pytest.fixture(autouse=True)
def _fresh_version_cache(monkeypatch):
    monkeypatch.setattr(dialect, "_versions", {})


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
