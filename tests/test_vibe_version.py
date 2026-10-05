"""Version-aware Vibe approval argv, mapped only where real wheels proved it.

Facts verified against installed wheels (AgentManager resolution and each
version's own argparse, no model calls): `default` resolves through 2.24.0 and
`ask` from 2.24.1 — the same approval-per-tool profile, swapped in 2.24.1 —
`--yolo` parses from 2.17.0, and `--agent auto-approve` resolves everywhere.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from shipped import pin_installed_vibe_version

from theater.harness import plan_launch
from theater.harness.builtin.plugins.vibe import version as version_module
from theater.harness.builtin.plugins.vibe.version import installed_vibe_version


def _plan(tmp_path, approval):
    return plan_launch(
        "vibe",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval=approval,
    )


@pytest.mark.parametrize(
    ("version", "agent"),
    [
        (None, "ask"),
        ((2, 13, 9), "default"),
        ((2, 14, 0), "default"),
        ((2, 16, 9), "default"),
        ((2, 17, 0), "default"),
        ((2, 24, 0), "default"),
        ((2, 24, 1), "ask"),
        ((2, 25, 0), "ask"),
    ],
)
def test_manual_maps_the_approval_per_tool_profile(tmp_path, monkeypatch, version, agent):
    pin_installed_vibe_version(monkeypatch, version)
    argv = _plan(tmp_path, "manual").argv
    assert f"--agent={agent}" in argv
    assert "--yolo" not in argv


@pytest.mark.parametrize(
    ("version", "yolo"),
    [
        (None, "flag"),
        ((2, 13, 9), "agent"),
        ((2, 14, 0), "agent"),
        ((2, 16, 9), "agent"),
        ((2, 17, 0), "flag"),
        ((2, 24, 9), "flag"),
        ((2, 25, 0), "flag"),
    ],
)
def test_yolo_takes_the_flag_only_where_it_exists(tmp_path, monkeypatch, version, yolo):
    pin_installed_vibe_version(monkeypatch, version)
    argv = _plan(tmp_path, "yolo").argv
    if yolo == "flag":
        assert "--yolo" in argv
        assert "--agent" not in argv
    else:
        assert argv[argv.index("--agent") + 1] == "auto-approve"
        assert "--yolo" not in argv


def test_edits_pins_accept_edits_on_every_version(tmp_path, monkeypatch):
    pin_installed_vibe_version(monkeypatch, (2, 13, 9))
    argv = _plan(tmp_path, "edits").argv
    assert argv[argv.index("--agent") + 1] == "accept-edits"


def test_probe_failure_keeps_todays_argv(tmp_path, monkeypatch):
    pin_installed_vibe_version(monkeypatch, None)
    assert "--agent=ask" in _plan(tmp_path, "manual").argv
    assert "--yolo" in _plan(tmp_path, "yolo").argv


@pytest.mark.parametrize(
    ("resolved", "argv0"),
    [("/opt/other-install/bin/vibe", "/opt/other-install/bin/vibe"), (None, "vibe")],
)
def test_launch_runs_the_binary_the_probe_resolved(tmp_path, monkeypatch, resolved, argv0):
    """The pane's PATH may differ from the daemon's, so argv[0] must be the probed path."""
    pin_installed_vibe_version(monkeypatch, (2, 25, 8), binary=resolved)
    assert _plan(tmp_path, "manual").argv[0] == argv0


def test_installed_version_parses_and_fails_closed(monkeypatch, tmp_path):
    binary = tmp_path / "vibe"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(version_module.shutil, "which", lambda name: str(binary))

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="vibe 2.24.1\n")

    monkeypatch.setattr(version_module.subprocess, "run", run)
    version_module._CACHE.clear()
    assert installed_vibe_version() == (2, 24, 1)

    monkeypatch.setattr(
        version_module.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="vibe 2.24.1"),
    )
    version_module._CACHE.clear()
    assert installed_vibe_version() is None

    def unavailable(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(version_module.subprocess, "run", unavailable)
    version_module._CACHE.clear()
    assert installed_vibe_version() is None

    # A failure is never cached: the next spawn re-probes and recovers.
    monkeypatch.setattr(version_module.subprocess, "run", run)
    assert installed_vibe_version() == (2, 24, 1)
