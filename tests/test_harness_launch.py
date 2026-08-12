"""The launch plans are pure data, so they can be asserted precisely.

These tests are the guard on the one thing phase 1a cannot get wrong: the
participant id must reach the MCP server through a channel the SDK's environment
allowlist cannot strip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theater.harness import plan_launch
from theater.models import BadRequest


def test_claude_carries_the_id_in_the_config_file(tmp_path):
    config = tmp_path / "abc.json"
    plan = plan_launch(
        "claude",
        participant_id="abc123",
        prompt="say hello",
        config_path=config,
        approval="manual",
    )

    assert plan.argv[:2] == ["claude", f"--mcp-config={config}"]
    assert plan.argv[-1] == "say hello"

    written = json.loads(plan.files[config])
    server = written["mcpServers"]["theater"]
    assert server["args"] == ["mcp", "--id", "abc123"]


def test_vibe_carries_the_id_in_an_env_override(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="abc123",
        prompt="say hello",
        config_path=tmp_path / "unused.json",
        approval="manual",
    )

    assert plan.argv == ["vibe", "say hello"]
    assert plan.files == {}

    servers = json.loads(plan.env["VIBE_MCP_SERVERS"])
    assert servers[0]["name"] == "theater"
    assert servers[0]["args"] == ["mcp", "--id", "abc123"]


def test_codex_carries_the_id_in_a_config_override(tmp_path):
    plan = plan_launch(
        "codex",
        participant_id="abc123",
        prompt="say hello",
        config_path=tmp_path / "unused.json",
        approval="manual",
    )

    assert plan.argv[0] == "codex"
    assert plan.argv[-1] == "say hello"
    # Nothing is written to disk and nothing rides in the environment: the
    # whole plan is argv.
    assert plan.files == {} and plan.env == {}
    # `-c` values are parsed as TOML, so both sides must be valid TOML literals.
    assert 'mcp_servers.theater.args=["mcp", "--id", "abc123"]' in plan.argv
    command = next(a for a in plan.argv if a.startswith("mcp_servers.theater.command="))
    assert command.endswith('"') and '="' in command


@pytest.mark.parametrize(
    "approval,expected",
    [
        ("manual", ["-a", "untrusted", "-s", "read-only"]),
        ("edits", ["-a", "on-request", "-s", "workspace-write"]),
        ("yolo", ["--dangerously-bypass-approvals-and-sandbox"]),
    ],
)
def test_codex_approval_modes_set_both_axes(tmp_path, approval, expected):
    """Approval and sandbox are independent, and neither may be inherited.

    With no flags codex falls back to ~/.codex/config.toml, which can say
    `never` / `danger-full-access` — so `manual` has to be explicit too.
    """
    plan = plan_launch(
        "codex",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval=approval,
    )
    assert all(flag in plan.argv for flag in expected)


def test_the_id_never_travels_by_bare_environment(tmp_path):
    """THEATER_ID alone is not a channel: the MCP SDK would strip it."""
    for harness in ("claude", "codex", "opencode", "vibe"):
        plan = plan_launch(
            harness,
            participant_id="abc123",
            prompt="",
            config_path=tmp_path / f"{harness}.json",
            approval="manual",
        )
        serialised = json.dumps(
            {"argv": plan.argv, "files": {str(k): v for k, v in plan.files.items()},
             "env": plan.env}
        )
        assert "abc123" in serialised


def test_empty_prompt_yields_no_positional(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="abc123",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
    )
    assert plan.argv == ["vibe"]


@pytest.mark.parametrize(
    "approval,expected",
    [("yolo", "--yolo"), ("edits", "accept-edits")],
)
def test_vibe_approval_modes(tmp_path, approval, expected):
    plan = plan_launch(
        "vibe",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval=approval,
    )
    assert expected in plan.argv


def test_unknown_harness_and_approval_are_rejected(tmp_path):
    with pytest.raises(BadRequest):
        plan_launch(
            "cursor", participant_id="a", prompt="", config_path=Path("/x"), approval="manual"
        )
    with pytest.raises(BadRequest):
        plan_launch(
            "vibe", participant_id="a", prompt="", config_path=Path("/x"), approval="whatever"
        )
