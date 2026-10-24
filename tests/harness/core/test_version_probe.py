"""Executable replacement and banner changes invalidate shared runtime probes."""

from __future__ import annotations

import os
import re
import subprocess

from theater.harness.contracts import version_probe


def test_a_replaced_executable_reprobes_by_identity(tmp_path, monkeypatch):
    binary = tmp_path / "agent"
    binary.write_text("executable")
    monkeypatch.setattr(version_probe.shutil, "which", lambda _: str(binary))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"agent 1.0.{len(calls)}", "")

    monkeypatch.setattr(version_probe.subprocess, "run", run)
    pattern = re.compile(r"agent (\S+)")
    assert version_probe.probe_release("agent", pattern, timeout=5) == "1.0.1"
    assert version_probe.probe_release("agent", pattern, timeout=5) == "1.0.1"
    identity = binary.stat()
    os.utime(binary, ns=(identity.st_atime_ns, identity.st_mtime_ns + 1))
    assert version_probe.probe_release("agent", pattern, timeout=5) == "1.0.2"
    binary.write_text("new executable")
    os.utime(binary, ns=(identity.st_atime_ns, identity.st_mtime_ns + 1))
    assert version_probe.probe_release("agent", pattern, timeout=5) == "1.0.3"
    assert len(calls) == 3


def test_patterns_do_not_share_cached_versions(tmp_path, monkeypatch):
    binary = tmp_path / "agent"
    binary.touch()
    monkeypatch.setattr(version_probe.shutil, "which", lambda _: str(binary))
    monkeypatch.setattr(
        version_probe.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "agent 1.0.0", ""),
    )

    assert version_probe.probe_release("agent", re.compile(r"agent (\S+)"), timeout=5) == "1.0.0"
    assert version_probe.probe_release("agent", re.compile(r"other (\S+)"), timeout=5) is None
