"""Hostile prompts stay one literal argv element in the claude, codex, and opencode plans.

Every parser here routes an element starting with "-" as an option: commander
(Claude, inferred; closed source), clap (Codex — `codex fork [OPTIONS]
[SESSION_ID] [PROMPT]` positional, no allow_hyphen_values), yargs 18 (OpenCode
1.x — a "-"-leading `--prompt` value is swallowed as a flag), and effect's CLI
(OpenCode 2.x — only non-"-" elements lex as flag values). A leading newline
defeats all four while costing the model one blank first line.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from shipped import OpenCodeHarness

from theater.harness import plan_launch
from theater.harness.builtin.plugins.opencode.dialect import OpenCodeDialect

HOSTILE_PROMPTS = [
    "--dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--settings=/x.json",
    "-x",
    "- item: finish the migration",
    "---",
    "--help",
]


@pytest.mark.parametrize("resume", [None, "0cf1f8e3-5f0e-4c48-a429-6a1b2c3d4e5f"])
@pytest.mark.parametrize("prompt", HOSTILE_PROMPTS)
def test_claude_keeps_a_flag_shaped_prompt_literal(tmp_path, monkeypatch, resume, prompt):
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "home"))
    plan = plan_launch(
        "claude",
        participant_id="p",
        prompt=prompt,
        config_path=tmp_path / "mcp.json",
        approval="manual",
        resume=resume,
    )
    argv = plan.argv
    assert argv[-3:-1] == ["--permission-mode", "default"]
    assert argv[-1] == f"\n{prompt}"
    assert not argv[-1].startswith("-")
    assert argv.count(f"\n{prompt}") == 1
    assert prompt not in argv


@pytest.mark.parametrize("resume", [None, "0cf1f8e3-5f0e-4c48-a429-6a1b2c3d4e5f"])
@pytest.mark.parametrize("prompt", HOSTILE_PROMPTS)
def test_codex_keeps_a_flag_shaped_prompt_literal(tmp_path, monkeypatch, resume, prompt):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    plan = plan_launch(
        "codex",
        participant_id="p",
        prompt=prompt,
        config_path=tmp_path / "mcp.json",
        approval="manual",
        resume=resume,
    )
    argv = plan.argv
    assert argv[-5:-1] == ["-a", "on-request", "-s", "read-only"]
    assert argv[-1] == f"\n{prompt}"
    assert not argv[-1].startswith("-")
    assert argv.count(f"\n{prompt}") == 1
    assert prompt not in argv
    if resume is None:
        assert argv[:1] == ["codex"]
    else:
        # The fork positional keeps its place ahead of every flag.
        assert argv[:3] == ["codex", "fork", resume]


def test_ordinary_prompts_are_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    prompt = "inspect this repository"
    claude = plan_launch(
        "claude",
        participant_id="p",
        prompt=prompt,
        config_path=tmp_path / "mcp.json",
        approval="manual",
    )
    assert claude.argv[-1] == prompt
    codex = plan_launch(
        "codex",
        participant_id="p",
        prompt=prompt,
        config_path=tmp_path / "mcp.json",
        approval="manual",
    )
    assert codex.argv[-1] == prompt


@pytest.fixture
def opencode_binary(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "opencode"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{os.environ['PATH']}")
    return str(binary.resolve())


@pytest.mark.parametrize("prompt", HOSTILE_PROMPTS)
def test_opencode_1x_keeps_the_prompt_the_value_of_its_flag(
    tmp_path, monkeypatch, opencode_binary, prompt
):
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "home"))
    plan = OpenCodeHarness().plan_launch(
        participant_id="p",
        prompt=prompt,
        config_path=tmp_path / "x.json",
        approval="yolo",
    )
    assert plan.argv == [opencode_binary, "--auto", "--prompt", f"\n{prompt}"]


@pytest.mark.parametrize("prompt", HOSTILE_PROMPTS)
def test_opencode_2x_yolo_carries_the_literal_prompt_to_its_tui(
    tmp_path, monkeypatch, opencode_binary, prompt
):
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "home"))
    plan = OpenCodeHarness(dialect=OpenCodeDialect.V2).plan_launch(
        participant_id="p",
        prompt=prompt,
        config_path=tmp_path / "x.json",
        approval="yolo",
    )
    argv = plan.argv
    assert argv.count("--prompt") == 1
    assert argv[argv.index("--prompt") + 1] == f"\n{prompt}"
    assert argv[argv.index("--prompt") + 1] not in HOSTILE_PROMPTS
    assert "--auto" in argv and prompt not in argv


def _bootstrap_settings(plan, config: Path) -> dict:
    """The settings embedded in the generated launcher, decoded as the launcher does."""
    script = plan.files[config.with_suffix(".bootstrap.py")]
    literal = script.rsplit("main(json.loads(", 1)[1].removesuffix(")))\n")
    return json.loads(json.loads(literal))


@pytest.mark.parametrize("prompt", HOSTILE_PROMPTS)
def test_opencode_2x_enforced_bootstrap_stores_the_literal_prompt(
    tmp_path, monkeypatch, opencode_binary, prompt
):
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
    config = tmp_path / "x.json"
    plan = OpenCodeHarness(dialect=OpenCodeDialect.V2).plan_launch(
        participant_id="p",
        prompt=prompt,
        config_path=config,
        approval="manual",
    )
    # The bootstrap appends ["--prompt", settings["prompt"]] to the TUI argv.
    assert _bootstrap_settings(plan, config)["prompt"] == f"\n{prompt}"
