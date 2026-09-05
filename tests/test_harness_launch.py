"""The launch plans are pure data, so they can be asserted precisely.

These tests are the guard on the one thing phase 1a cannot get wrong: the
participant id must reach the MCP server through a channel the SDK's environment
allowlist cannot strip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shipped import VibeHarness

from theater import paths
from theater.harness import HARNESSES, Harness, LaunchPlan, plan_launch, theater_mcp_servers
from theater.harness.base import theater_binary
from theater.harness.builtin.plugins.vibe.constants import ISOLATION_MARKER
from theater.harness.builtin.plugins.vibe.isolation import validate_isolated_domain
from theater.mcp_plugins import McpServerSpec
from theater.models import BadRequest


def test_vibe_carries_the_id_in_an_env_override(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="abc123",
        prompt="say hello",
        config_path=tmp_path / "unused.json",
        approval="manual",
    )

    assert plan.argv == ["vibe", "say hello"]
    assert list(plan.files) == [Path(plan.env["VIBE_SESSION_LOGGING__SAVE_DIR"]) / ISOLATION_MARKER]

    servers = json.loads(plan.env["VIBE_MCP_SERVERS"])
    assert [server["name"] for server in servers] == ["theater", "theater_wait"]
    assert servers[0]["args"] == [
        "mcp",
        "--id",
        "abc123",
        "--harness",
        "vibe",
        "--toolset",
        "control",
    ]
    assert servers[1]["args"] == [
        "mcp",
        "--id",
        "abc123",
        "--harness",
        "vibe",
        "--toolset",
        "wait",
    ]
    assert servers[0]["command"] == servers[1]["command"]


def test_core_mcp_specs_use_resolved_executable_and_separate_toolsets():
    servers = theater_mcp_servers("abc123", "codex")

    assert [server.name for server in servers] == ["theater", "theater_wait"]
    assert all(server.command == theater_binary() for server in servers)
    assert servers[0].args == (
        "mcp",
        "--id",
        "abc123",
        "--harness",
        "codex",
        "--toolset",
        "control",
    )
    assert servers[1].args[-1] == "wait"


def test_vibe_cold_spawn_always_gets_an_isolated_transcript_domain(tmp_path, monkeypatch):
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))

    plan = plan_launch(
        "vibe",
        participant_id="first",
        prompt="",
        config_path=tmp_path / "first.json",
        approval="manual",
    )

    save_dir = Path(plan.env["VIBE_SESSION_LOGGING__SAVE_DIR"])
    assert save_dir == paths.participant_observation_dir("first", "vibe")
    assert plan.transcript_domain == str(save_dir.resolve())
    assert list(plan.files) == [save_dir / ISOLATION_MARKER]

    save_dir.mkdir(parents=True)
    (save_dir / ISOLATION_MARKER).write_text(plan.files[save_dir / ISOLATION_MARKER])
    marker = validate_isolated_domain(save_dir, participant_id="first")
    assert marker is not None
    assert marker["transcript_domain"] == str(save_dir.resolve())


def test_vibe_manifest_launch_uses_its_injected_correlation_root(tmp_path):
    correlation_root = tmp_path / "correlation"
    plan = VibeHarness(correlation_root=correlation_root).plan_launch(
        participant_id="first",
        prompt="",
        config_path=tmp_path / "first.json",
        approval="manual",
    )

    save_dir = correlation_root / "first"
    assert Path(plan.env["VIBE_SESSION_LOGGING__SAVE_DIR"]) == save_dir
    assert plan.transcript_domain == str(save_dir.resolve())
    assert list(plan.files) == [save_dir / ISOLATION_MARKER]
    assert json.loads(plan.files[save_dir / ISOLATION_MARKER])["transcript_domain"] == str(
        save_dir.resolve()
    )


def test_vibe_outlasts_a_full_length_await(tmp_path):
    """The MCP tool timeout must exceed the daemon's own await ceiling.

    Vibe's default is 60s, so a `jobs.await` that legitimately blocks for
    minutes dies on the wire and the agent concludes awaiting is broken.
    Asserted against MAX_AWAIT rather than a literal so the two cannot drift
    apart silently.
    """
    from theater.daemon.methods import MAX_AWAIT

    plan = plan_launch(
        "vibe",
        participant_id="abc123",
        prompt="say hello",
        config_path=tmp_path / "unused.json",
        approval="manual",
    )

    servers = json.loads(plan.env["VIBE_MCP_SERVERS"])
    assert len(servers) == 2
    assert all(server["tool_timeout_sec"] > MAX_AWAIT for server in servers)


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
    assert (
        'mcp_servers.theater.args=["mcp", "--id", "abc123", "--harness", '
        '"codex", "--toolset", "control"]'
    ) in plan.argv
    assert (
        'mcp_servers.theater_wait.args=["mcp", "--id", "abc123", "--harness", '
        '"codex", "--toolset", "wait"]'
    ) in plan.argv
    commands = [a for a in plan.argv if a.startswith("mcp_servers.") and ".command=" in a]
    assert len(commands) == 2
    assert all(command.endswith('"') and '="' in command for command in commands)


@pytest.mark.parametrize(
    ("harness", "approval"),
    (
        ("claude", "manual"),
        ("codex", "manual"),
        ("opencode", "manual"),
        ("vibe", "manual"),
        ("pi", "yolo"),
    ),
)
def test_shipped_renderers_receive_core_and_extra_stdio_servers(tmp_path, harness, approval):
    """Every shipped harness renders the shared endpoints plus an arbitrary extension."""
    extra = McpServerSpec(
        name="metrics",
        command="metrics-mcp",
        args=("--scope", "participant"),
        env={"METRICS_TOKEN": "launch-local", "METRICS_LABEL": "needs quotes"},
    )
    plan = plan_launch(
        harness,
        participant_id="abc123",
        prompt="",
        config_path=tmp_path / f"{harness}.json",
        approval=approval,
        mcp_servers=(*theater_mcp_servers("abc123", harness), extra),
    )

    if harness in {"claude", "pi"}:
        servers = json.loads(plan.files[tmp_path / f"{harness}.json"])["mcpServers"]
        assert servers["metrics"] == {
            "command": "metrics-mcp",
            "args": ["--scope", "participant"],
            "env": {"METRICS_TOKEN": "launch-local", "METRICS_LABEL": "needs quotes"},
        }
    elif harness == "codex":
        assert 'mcp_servers.metrics.command="metrics-mcp"' in plan.argv
        assert 'mcp_servers.metrics.args=["--scope", "participant"]' in plan.argv
        assert [value for value in plan.argv if value.startswith("mcp_servers.metrics.env.")] == [
            'mcp_servers.metrics.env.METRICS_TOKEN="launch-local"',
            'mcp_servers.metrics.env.METRICS_LABEL="needs quotes"',
        ]
        assert not any(value.startswith("mcp_servers.metrics.env={") for value in plan.argv)
        servers = {"theater", "theater_wait", "metrics"}
    elif harness == "opencode":
        servers = json.loads(plan.files[tmp_path / "opencode.json"])["mcp"]
        assert servers["metrics"] == {
            "type": "local",
            "enabled": True,
            "command": ["metrics-mcp", "--scope", "participant"],
            "environment": {"METRICS_TOKEN": "launch-local", "METRICS_LABEL": "needs quotes"},
        }
    else:
        entries = json.loads(plan.env["VIBE_MCP_SERVERS"])
        servers = {entry["name"]: entry for entry in entries}
        assert servers["metrics"] == {
            "name": "metrics",
            "transport": "stdio",
            "command": "metrics-mcp",
            "args": ["--scope", "participant"],
            "tool_timeout_sec": 340.0,
            "env": {"METRICS_TOKEN": "launch-local", "METRICS_LABEL": "needs quotes"},
        }
    assert set(servers) == {"theater", "theater_wait", "metrics"}


def test_manifest_without_a_renderer_ignores_stdio_servers(tmp_path):
    """An older manifest keeps its base plan when the runtime supplies endpoints."""
    from dataclasses import replace

    from theater.harness.builtin.plugins.codex.manifest import MANIFEST
    from theater.harness.manifests.compiler import compile_manifest

    harness = compile_manifest("no-renderer", replace(MANIFEST, mcp=None))
    plan = harness.plan_launch(
        participant_id="abc123",
        prompt="",
        config_path=tmp_path / "unused.json",
        approval="manual",
        mcp_servers=(McpServerSpec(name="metrics", command="metrics-mcp"),),
    )
    assert not any(argument.startswith("mcp_servers.") for argument in plan.argv)


def test_manifest_renderer_rejects_a_malformed_result(tmp_path):
    """Renderer callbacks can add only an McpRenderOverlay."""
    from dataclasses import replace

    from theater.harness.builtin.plugins.codex.manifest import MANIFEST
    from theater.harness.contracts.manifest import McpRenderingManifest
    from theater.harness.manifests.compiler import compile_manifest

    harness = compile_manifest(
        "bad-renderer",
        replace(MANIFEST, mcp=McpRenderingManifest(renderer=lambda _context: LaunchPlan(argv=[]))),
    )
    with pytest.raises(TypeError, match="MCP renderer must return an McpRenderOverlay"):
        harness.plan_launch(
            participant_id="abc123",
            prompt="",
            config_path=tmp_path / "unused.json",
            approval="manual",
            mcp_servers=(McpServerSpec(name="metrics", command="metrics-mcp"),),
        )


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
        "claude",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
        model="opus-4.1",
    )
    assert "--model=opus-4.1" in plan.argv

    plan = plan_launch(
        "codex",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
        model="gpt-5",
    )
    assert plan.argv[plan.argv.index("--model") :][:2] == ["--model", "gpt-5"]

    plan = plan_launch(
        "opencode",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
        model="anthropic/claude-sonnet-4",
    )
    assert plan.argv[plan.argv.index("--model") :][:2] == [
        "--model",
        "anthropic/claude-sonnet-4",
    ]

    plan = plan_launch(
        "vibe",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
        model="mistral-large",
    )
    assert plan.env["VIBE_ACTIVE_MODEL"] == "mistral-large"
    assert not any(a.startswith("--model") for a in plan.argv)


def test_claude_launch_adds_receipt_hooks_without_editing_user_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    plan = plan_launch(
        "claude",
        participant_id="abc123",
        prompt="",
        config_path=tmp_path / "mcp.json",
        approval="manual",
    )

    settings_arg = next(arg for arg in plan.argv if arg.startswith("--settings="))
    settings_path = Path(settings_arg.removeprefix("--settings="))
    settings = json.loads(plan.files[settings_path])

    assert settings_path == paths.participant_launch_dir("abc123") / "claude.settings.json"
    assert plan.receipt_token_path is not None
    assert plan.receipt_token_path.name == "receipt-token"
    assert plan.receipt_token is None  # core mints the token, not the plugin
    assert not plan.private_files  # core owns the token file, not the plugin
    assert set(settings["hooks"]) == {"SessionStart", "PreCompact"}
    assert "Stop" not in settings["hooks"]
    for entries in settings["hooks"].values():
        command = entries[0]["hooks"][0]["command"]
        assert "transcript-receipt" in command
        assert "--id abc123" in command
        assert str(plan.receipt_token_path) in command


def test_no_model_asked_means_no_model_flag(tmp_path):
    for harness in ("claude", "codex", "opencode", "vibe"):
        plan = plan_launch(
            harness,
            participant_id="a",
            prompt="",
            config_path=tmp_path / "x.json",
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
        "vibe",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
    )
    assert plan.env["VIBE_ACTIVE_MODEL"] == ""


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
        "legacy",
        participant_id="abc123",
        prompt="",
        config_path=Path("/x"),
        approval="manual",
        mcp_servers=(McpServerSpec(name="metrics", command="metrics-mcp"),),
    )
    assert plan.argv == ["legacy", "abc123"]

    with pytest.raises(BadRequest, match="does not support model selection"):
        plan_launch(
            "legacy",
            participant_id="abc123",
            prompt="",
            config_path=Path("/x"),
            approval="manual",
            model="anything",
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


# ---- reasoning effort reaches the harnesses that support it --------------


def test_reasoning_effort_reaches_codex_via_config_override(tmp_path):
    plan = plan_launch(
        "codex",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
        reasoning_effort="high",
    )
    assert "model_reasoning_effort=high" in plan.argv


def test_reasoning_effort_reaches_claude_via_effort_flag(tmp_path):
    plan = plan_launch(
        "claude",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
        reasoning_effort="high",
    )
    assert "--effort=high" in plan.argv


def test_no_reasoning_effort_asked_means_no_lever(tmp_path):
    for harness in ("claude", "codex", "opencode", "vibe"):
        plan = plan_launch(
            harness,
            participant_id="a",
            prompt="",
            config_path=tmp_path / "x.json",
            approval="manual",
        )
        assert not any(a.startswith("--effort") for a in plan.argv), harness
        assert not any("model_reasoning_effort=" in a for a in plan.argv), harness


def test_a_harness_that_does_not_support_reasoning_is_refused(tmp_path):
    """Vibe and opencode omit the parameter; the funnel refuses before the adapter."""
    with pytest.raises(BadRequest, match="does not support reasoning effort"):
        plan_launch(
            "vibe",
            participant_id="a",
            prompt="",
            config_path=tmp_path / "x.json",
            approval="manual",
            reasoning_effort="high",
        )
    with pytest.raises(BadRequest, match="does not support reasoning effort"):
        plan_launch(
            "opencode",
            participant_id="a",
            prompt="",
            config_path=tmp_path / "x.json",
            approval="manual",
            reasoning_effort="high",
        )


def test_a_legacy_harness_still_launches_without_reasoning(monkeypatch, tmp_path):
    """An adapter written before reasoning_effort existed is never called with it."""

    class LegacyHarness(Harness):
        name = "legacy2"
        binary = "legacy2"

        def plan_launch(self, *, participant_id, prompt, config_path, approval):
            return LaunchPlan(argv=["legacy2", participant_id])

    monkeypatch.setitem(HARNESSES, "legacy2", LegacyHarness())

    plan = plan_launch(
        "legacy2",
        participant_id="abc",
        prompt="",
        config_path=Path("/x"),
        approval="manual",
    )
    assert plan.argv == ["legacy2", "abc"]
