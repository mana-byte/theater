"""Launch version probes never trust a cached entrypoint identity."""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from theater.harness.contracts import version_probe


def test_same_size_same_mtime_replacement_is_probed_afresh(tmp_path, monkeypatch):
    binary = tmp_path / "agent"
    binary.write_text("version 1")
    identity = binary.stat()
    monkeypatch.setattr(version_probe.shutil, "which", lambda _: str(binary))

    def run(argv, **kwargs):
        version = "1.0.1" if binary.read_text() == "version 1" else "9.0.0"
        return subprocess.CompletedProcess(argv, 0, f"agent {version}", "")

    monkeypatch.setattr(version_probe.subprocess, "run", run)
    pattern = re.compile(r"agent (\S+)")
    assert version_probe.probe_release("agent", pattern, timeout=5) == "1.0.1"
    binary.write_text("version 9")
    os.utime(binary, ns=(identity.st_atime_ns, identity.st_mtime_ns))
    assert binary.stat().st_size == identity.st_size
    assert version_probe.probe_release("agent", pattern, timeout=5) == "9.0.0"


def test_banner_patterns_and_stderr_are_respected(monkeypatch):
    monkeypatch.setattr(
        version_probe.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "agent 1.0.0"),
    )
    assert version_probe.probe_release("agent", re.compile(r"agent (\S+)"), timeout=5) == "1.0.0"
    assert version_probe.probe_release("agent", re.compile(r"other (\S+)"), timeout=5) is None


def test_a_failed_exit_cannot_supply_a_version(monkeypatch):
    monkeypatch.setattr(
        version_probe.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "agent 1.0.0", "crashed"),
    )
    with pytest.raises(subprocess.CalledProcessError):
        version_probe.probe_release("agent", re.compile(r"agent (\S+)"), timeout=5)
