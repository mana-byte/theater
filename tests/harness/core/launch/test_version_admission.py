"""Provider launch routes cannot bypass the shipped CLI support policy."""

from __future__ import annotations

import importlib
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.version_policy import VersionPolicy, version_key
from theater.models import BadRequest


def _refused_releases():
    plugins = Path(__file__).parents[4] / "theater/harness/builtin/plugins"
    cases = []
    for name in ("claude", "codex", "opencode", "pi", "vibe"):
        policy = VersionPolicy.load(plugins / name / "compatibility.json")
        maximum = version_key(policy.maximum)
        versions = [
            # A neutral below-floor release: never derived from the moving minimum.
            ("below-floor", "0.0.1"),
            ("above-maximum", ".".join(map(str, (*maximum[:2], maximum[2] + 1)))),
            # A prerelease banner is refused as unstable, never as out of range.
            ("prerelease", f"{policy.minimum}-beta"),
            *((f"excluded-{version}", version) for version in policy.excluded),
        ]
        cases.extend(
            pytest.param(
                name,
                version,
                (
                    r"stable|usable"
                    if label == "prerelease"
                    else f"{re.escape(policy.range_text)}.*tell the user to"
                ),
                id=f"{name}-{label}",
            )
            for label, version in versions
        )
    return cases


@pytest.mark.parametrize("name,version,expected", _refused_releases())
def test_unqualified_cli_is_refused_on_every_provider_launch(
    tmp_path, monkeypatch, name, version, expected
):
    binary = tmp_path / name
    binary.write_text("#!/bin/sh\nexit 1\n")
    binary.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda _: str(binary))
    monkeypatch.delenv("THEATER_OPENCODE_VERSION", raising=False)
    output = {
        "claude": f"{version} (Claude Code)",
        "codex": f"codex-cli {version}",
        "opencode": f"opencode {version}",
        "pi": version,
        "vibe": f"vibe {version}",
    }[name]

    def run(argv, **_kwargs):
        assert argv == [str(binary), "--version"]
        return SimpleNamespace(stdout=output, stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    planner = importlib.import_module(f"theater.harness.builtin.plugins.{name}.launch").plan_launch
    context = LaunchContext(
        "participant", "hello", tmp_path / "mcp.json", "manual", binary=str(binary)
    )
    with pytest.raises(BadRequest, match=expected):
        planner(context)
