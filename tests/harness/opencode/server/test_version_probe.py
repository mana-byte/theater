"""OpenCode version and flag probes: qualified windows, failures, cached results."""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.opencode_probe import ProbeResult, patch_probe
from theater.daemon.spawning import planning
from theater.daemon.spawning.models import SpawnRequest
from theater.harness.builtin.plugins.opencode import dialect, server_plan
from theater.harness.builtin.plugins.opencode.dialect import (
    installed_version,
    require_supported_version,
    resolve_binary,
)
from theater.harness.builtin.plugins.opencode.runtime_plan import (
    OPENCODE_SERVER_COMPATIBILITY_POLICY,
    VERSION_POLICY,
    release_text,
)
from theater.harness.builtin.plugins.opencode.server_plan import (
    probe_opencode_server_compatibility,
)
from theater.harness.contracts import version_probe
from theater.harness.contracts.runtime import (
    RuntimeProbeContext,
)
from theater.harness.contracts.version_policy import version_key
from theater.models import BadRequest


@pytest.mark.parametrize("returncodes", [(0, 0), (1, 0), (0, 1)])
def test_probe_requires_both_successful_checks(
    monkeypatch: pytest.MonkeyPatch, returncodes: tuple[int, int]
) -> None:
    patch_probe(
        monkeypatch, f"opencode v{VERSION_POLICY.minimum}\n", "--port --hostname", returncodes
    )
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is (returncodes == (0, 0))
    if compatibility.supported:
        assert compatibility.native_version == VERSION_POLICY.minimum


def _patch_offset(version: tuple[int, int, int], offset: int) -> str:
    return release_text((*version[:2], version[2] + offset))


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        (_patch_offset(version_key(VERSION_POLICY.minimum), -1), False),
        (VERSION_POLICY.minimum, True),
        (VERSION_POLICY.maximum, True),
        (_patch_offset(version_key(VERSION_POLICY.maximum), 1), False),
        ("1.18.29", False),
    ],
)
def test_probe_qualifies_only_the_shared_json_policy(
    monkeypatch: pytest.MonkeyPatch, version: str, supported: bool
) -> None:
    patch_probe(monkeypatch, f"opencode v{version}\n", "--port --hostname")
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is supported
    assert compatibility.policy == OPENCODE_SERVER_COMPATIBILITY_POLICY
    assert compatibility.native_version == version
    if not supported:
        assert VERSION_POLICY.range_text in compatibility.reason


def test_probe_requires_the_serve_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_probe(monkeypatch, VERSION_POLICY.minimum, "--model --auto")
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False
    assert "port-0 flags" in (compatibility.reason or "")


def test_a_2x_resume_probe_refuses_before_any_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume continues in the lineage database, which the native backend cannot open."""
    patch_probe(monkeypatch, f"opencode v{VERSION_POLICY.minimum}\n", "--port --hostname")
    refused = probe_opencode_server_compatibility(
        RuntimeProbeContext(binary="opencode", resume=True)
    )
    assert refused.supported is False
    assert "provider route" in (refused.reason or "")


def test_an_unsupported_resume_cannot_select_a_provider_fallback(monkeypatch):
    patch_probe(monkeypatch, "opencode v1.18.29\n", "--port --hostname")
    refused = probe_opencode_server_compatibility(RuntimeProbeContext(resume=True))
    assert not refused.supported
    assert VERSION_POLICY.range_text in refused.reason
    assert "provider route" not in refused.reason


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
            return ProbeResult(
                VERSION_POLICY.minimum if argv[-1] == "--version" else "--port --hostname"
            )
        finally:
            finished.add(argv[-1])

    monkeypatch.setattr(server_plan.subprocess, "run", run)
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False
    assert "probes" in (compatibility.reason or "")
    assert finished == {"--version", "--help"}


@pytest.mark.parametrize("version", ["2.0.18-beta.1", "2.0.18+build", "02.0.18", "unknown"])
def test_probe_rejects_noncanonical_or_unstable_releases(monkeypatch, version):
    patch_probe(monkeypatch, version, "--port --hostname")
    refused = probe_opencode_server_compatibility(RuntimeProbeContext())
    assert not refused.supported and refused.native_version is None


def test_resolve_binary_pins_the_absolute_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "opencode"
    binary.write_text(f"#!/bin/sh\necho {VERSION_POLICY.minimum}\n")
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
        require_supported_version("opencode")
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
    monkeypatch.setenv(dialect.VERSION_ENV, VERSION_POLICY.minimum)
    assert require_supported_version("opencode") == version_key(VERSION_POLICY.minimum)


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
    monkeypatch.setattr(version_probe, "_FAILURE_BACKOFF", 0.05)
    clock = [1000.0]
    monkeypatch.setattr(version_probe.time, "monotonic", lambda: clock[0])

    assert installed_version("opencode") is None
    assert len(counter.read_text().splitlines()) == 1

    # Repair the CLI with the same size and mtime: only the failure's expiry
    # (not a file-identity change) can drive the re-probe.
    repaired = f'#!/bin/sh\necho run >> "{counter}"\necho {VERSION_POLICY.minimum}\n'
    padding = identity.st_size - len(repaired.encode())
    assert padding >= 2
    repaired += "#" + "x" * (padding - 2) + "\n"
    binary.write_text(repaired)
    os.utime(binary, ns=(identity.st_atime_ns, identity.st_mtime_ns))
    assert binary.stat().st_size == identity.st_size

    clock[0] += 0.1
    assert installed_version("opencode") == version_key(VERSION_POLICY.minimum)
    assert len(counter.read_text().splitlines()) == 2
