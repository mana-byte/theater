"""The launch plans are pure data, so they can be asserted precisely.

These tests are the guard on the one thing phase 1a cannot get wrong: the
participant id must reach the MCP server through a channel the SDK's environment
allowlist cannot strip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theater.harness import HARNESSES, Harness, LaunchPlan, plan_launch
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


def test_model_reaches_every_harness_by_its_own_lever(tmp_path):
    """Three CLIs take a flag, one takes an environment variable.

    Asserted per harness rather than by searching the whole plan, because
    "the string is in there somewhere" would pass if a model ended up in the
    prompt positional.
    """
    plan = plan_launch(
        "claude", participant_id="a", prompt="", config_path=tmp_path / "x.json",
        approval="manual", model="opus-4.1",
    )
    assert "--model=opus-4.1" in plan.argv

    plan = plan_launch(
        "codex", participant_id="a", prompt="", config_path=tmp_path / "x.json",
        approval="manual", model="gpt-5",
    )
    assert plan.argv[plan.argv.index("--model") :][:2] == ["--model", "gpt-5"]

    plan = plan_launch(
        "opencode", participant_id="a", prompt="", config_path=tmp_path / "x.json",
        approval="manual", model="anthropic/claude-sonnet-4",
    )
    assert plan.argv[plan.argv.index("--model") :][:2] == [
        "--model",
        "anthropic/claude-sonnet-4",
    ]

    plan = plan_launch(
        "vibe", participant_id="a", prompt="", config_path=tmp_path / "x.json",
        approval="manual", model="mistral-large",
    )
    assert plan.env["VIBE_ACTIVE_MODEL"] == "mistral-large"
    assert not any(a.startswith("--model") for a in plan.argv)


def test_no_model_asked_means_no_model_flag(tmp_path):
    for harness in ("claude", "codex", "opencode", "vibe"):
        plan = plan_launch(
            harness, participant_id="a", prompt="", config_path=tmp_path / "x.json",
            approval="manual",
        )
        assert not any(a.startswith("--model") for a in plan.argv), harness


def test_vibe_pins_the_model_env_even_when_none_was_asked_for(tmp_path):
    """The flag harnesses get this for free; the env one has to be told.

    VIBE_ACTIVE_MODEL is inherited by anything the pane starts, so a vibe agent
    spawned with a model would hand it to every descendant that did not ask for
    one. Writing the variable empty is what stops that, and it is the whole
    reason this harness sets it unconditionally.
    """
    plan = plan_launch(
        "vibe", participant_id="a", prompt="", config_path=tmp_path / "x.json",
        approval="manual",
    )
    assert plan.env["VIBE_ACTIVE_MODEL"] == ""


def test_model_names_are_not_validated(tmp_path):
    """Vendor namespaces churn; an allowlist here would be wrong within a month.

    A nonsense name is Theater's business to carry, not to judge — it fails in
    the pane, where the CLI that owns the namespace can say so.
    """
    plan = plan_launch(
        "claude", participant_id="a", prompt="", config_path=tmp_path / "x.json",
        approval="manual", model="mysuperdupermodelname",
    )
    assert "--model=mysuperdupermodelname" in plan.argv


def test_a_harness_that_predates_model_selection_still_launches(monkeypatch, tmp_path):
    """The compatibility contract for third-party plugins, in one test.

    An adapter written before `model` existed is never called with the keyword,
    so every model-less launch keeps working. Asking it for a model is refused
    by name — not as a TypeError from inside the plugin, and not by silently
    dropping the caller's choice and starting the wrong model.
    """

    class LegacyHarness(Harness):
        name = "legacy"
        binary = "legacy"

        def plan_launch(self, *, participant_id, prompt, config_path, approval):
            return LaunchPlan(argv=["legacy", participant_id])

    monkeypatch.setitem(HARNESSES, "legacy", LegacyHarness())

    plan = plan_launch(
        "legacy", participant_id="abc123", prompt="", config_path=Path("/x"),
        approval="manual",
    )
    assert plan.argv == ["legacy", "abc123"]

    with pytest.raises(BadRequest, match="does not support model selection"):
        plan_launch(
            "legacy", participant_id="abc123", prompt="", config_path=Path("/x"),
            approval="manual", model="anything",
        )


def test_unknown_harness_and_approval_are_rejected(tmp_path):
    with pytest.raises(BadRequest):
        plan_launch(
            "cursor", participant_id="a", prompt="", config_path=Path("/x"), approval="manual"
        )
    with pytest.raises(BadRequest):
        plan_launch(
            "vibe", participant_id="a", prompt="", config_path=Path("/x"), approval="whatever"
        )
