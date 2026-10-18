"""Claude launch planning: argv, MCP config, resume fork, hook settings and thinking defaults."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from tests.support.shipped import ClaudeCodeHarness
from theater import paths
from theater.daemon.spawning.planning import install_hook_plan
from theater.harness import theater_mcp_servers
from theater.harness.builtin.plugins.claude.constants import (
    CLAUDE_RECEIPT_EVENTS,
    CLAUDE_THINKING_SUMMARIES_KEY,
)
from theater.harness.builtin.plugins.claude.hooks import (
    NATIVE_HOOK_CHANNEL,
    install_native_hooks,
    native_hook_settings,
    native_hook_token_path,
)
from theater.harness.builtin.plugins.claude.launch import (
    plan_launch,
)
from theater.harness.builtin.plugins.claude.manifest import MANIFEST
from theater.harness.contracts.callbacks import (
    HookInstallContext,
    LaunchContext,
)
from theater.harness.contracts.channels import ChannelKind
from theater.harness.manifests.compiler import compile_manifest
from theater.models import Participant


def test_plan_env_pins_config_dir_only_when_configured(tmp_path, monkeypatch) -> None:
    def context() -> LaunchContext:
        return LaunchContext(
            participant_id="abc123",
            prompt="hi",
            config_path=tmp_path / "mcp.json",
            approval="manual",
        )

    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    plan = plan_launch(context())
    assert plan.env["CLAUDE_CONFIG_DIR"] == str(config)

    # A relative value is pinned absolute: the child's cwd is not the daemon's.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "claude-work")
    plan = plan_launch(context())
    assert plan.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-work")

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    plan = plan_launch(context())
    assert "CLAUDE_CONFIG_DIR" not in plan.env
    # Past the 340 s tool timeout, so a long await is never moved to the background.
    assert plan.env["CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"] == "360000"


def test_plan_launch_with_resume_forks_to_a_fresh_session(tmp_path):
    plan = ClaudeCodeHarness().plan_launch(
        participant_id="p-abc",
        prompt="continue",
        config_path=tmp_path / "mcp.json",
        approval="manual",
        resume="session-uuid-123",
    )
    assert "--resume=session-uuid-123" in plan.argv
    assert "--fork-session" in plan.argv
    assert f"--session-id={plan.session_id}" in plan.argv
    assert plan.session_id != "session-uuid-123"
    assert plan.argv[-1] == "continue"


def test_plan_launch_without_resume_adds_no_resume_flag(tmp_path):
    plan = ClaudeCodeHarness().plan_launch(
        participant_id="p-abc",
        prompt="hello",
        config_path=tmp_path / "mcp.json",
        approval="manual",
    )
    assert not any(arg.startswith("--resume") for arg in plan.argv)
    assert "--fork-session" not in plan.argv


def test_launch_composes_receipt_and_async_native_hook_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    participant_id = "claude-hooks"
    plan = plan_launch(
        LaunchContext(
            participant_id=participant_id,
            prompt="",
            config_path=tmp_path / "mcp.json",
            approval="manual",
        )
    )
    settings_path = next(path for path in plan.files if path.name == "claude.settings.json")
    native = native_hook_settings(participant_id)["hooks"]
    assert not set(native).intersection(json.loads(plan.files[settings_path])["hooks"])
    harness = compile_manifest("claude", MANIFEST)
    installed = install_hook_plan(
        plan,
        Participant(id=participant_id, harness="claude"),
        harness.observer,
        enabled_channels=frozenset({NATIVE_HOOK_CHANNEL}),
    )
    settings = json.loads(installed.files[settings_path])
    assert settings[CLAUDE_THINKING_SUMMARIES_KEY] is True
    assert isinstance(native, dict)
    for event, entries in native.items():
        assert settings["hooks"][event] == entries
        [entry] = entries
        [hook] = entry["hooks"]
        assert hook["type"] == "command"
        assert hook["async"] is True
        assert "--strict-exit" not in hook["command"]
        assert shlex.split(hook["command"])[1:3] == ["harness-event", event]

    [credential] = installed.channel_credentials
    assert credential.kind is ChannelKind.HOOK
    assert credential.channel_id == NATIVE_HOOK_CHANNEL
    assert credential.token_path == native_hook_token_path(participant_id)
    assert credential.token_path == (
        paths.participant_observation_dir(participant_id, "claude") / "hook-native-hooks.token"
    )


@pytest.mark.parametrize(
    ("user_settings", "expected", "resume"),
    [
        pytest.param(None, True, None, id="absent"),
        pytest.param('{"showThinkingSummaries": false}', None, None, id="false"),
        pytest.param('{"showThinkingSummaries": true}', None, None, id="true"),
        pytest.param("{malformed", True, "prior-session", id="malformed-resume"),
        pytest.param("[]", True, None, id="non-object"),
    ],
)
def test_launch_defaults_thinking_summaries_only_without_a_user_choice(
    tmp_path,
    monkeypatch,
    user_settings: str | None,
    expected: bool | None,
    resume: str | None,
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    settings_path = tmp_path / "home" / ".claude" / "settings.json"
    if user_settings is not None:
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(user_settings, encoding="utf-8")

    plan = plan_launch(
        LaunchContext("claude-thinking", "", tmp_path / "mcp.json", "manual", resume=resume)
    )
    launch_settings_path = next(path for path in plan.files if path.name == "claude.settings.json")
    launch_settings = json.loads(plan.files[launch_settings_path])

    assert launch_settings.get(CLAUDE_THINKING_SUMMARIES_KEY) is expected
    assert set(launch_settings["hooks"]) == set(CLAUDE_RECEIPT_EVENTS)
    if resume is not None:
        assert [f"--resume={resume}", "--fork-session"] == plan.argv[4:6]


def test_launch_uses_nonempty_claude_config_dir_for_the_user_choice(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "custom-claude"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({CLAUDE_THINKING_SUMMARIES_KEY: False}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    plan = plan_launch(LaunchContext("claude-thinking", "", tmp_path / "mcp.json", "manual"))
    launch_settings_path = next(path for path in plan.files if path.name == "claude.settings.json")

    assert CLAUDE_THINKING_SUMMARIES_KEY not in json.loads(plan.files[launch_settings_path])


def test_unreadable_user_settings_do_not_block_the_launch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    settings_path = tmp_path / "home" / ".claude" / "settings.json"
    original_read_text = Path.read_text

    def unreadable(path: Path, *args, **kwargs) -> str:
        if path == settings_path:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    plan = plan_launch(LaunchContext("claude-thinking", "", tmp_path / "mcp.json", "manual"))
    launch_settings_path = next(path for path in plan.files if path.name == "claude.settings.json")

    assert json.loads(plan.files[launch_settings_path])[CLAUDE_THINKING_SUMMARIES_KEY] is True


def test_native_hook_installer_refuses_a_token_path_other_than_its_command_path(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    with pytest.raises(ValueError, match="does not match"):
        install_native_hooks(
            HookInstallContext(
                participant_id="claude-hooks",
                channel_id=NATIVE_HOOK_CHANNEL,
                token_file=tmp_path / "wrong-token",
                theater_executable="theater",
            )
        )


def _argv(approval: str, tmp_path: Path) -> list[str]:
    plan = ClaudeCodeHarness().plan_launch(
        participant_id="p-abc",
        prompt="say hello",
        config_path=tmp_path / "mcp.json",
        approval=approval,
    )
    return plan.argv


def test_the_mcp_config_is_bound_with_an_equals_sign(tmp_path):
    """`--mcp-config <path>` is variadic and swallows the prompt positional."""
    argv = _argv("manual", tmp_path)
    assert argv[1] == f"--mcp-config={tmp_path / 'mcp.json'}"
    assert argv[-1] == "say hello"


def test_an_empty_prompt_leaves_claude_waiting_rather_than_running_nothing(tmp_path):
    plan = ClaudeCodeHarness().plan_launch(
        participant_id="p-abc",
        prompt="",
        config_path=tmp_path / "mcp.json",
        approval="manual",
    )
    assert plan.argv[:2] == ["claude", f"--mcp-config={tmp_path / 'mcp.json'}"]
    assert plan.argv[2].startswith("--settings=")
    assert plan.argv[3] == f"--session-id={plan.session_id}"
    assert plan.argv[4:6] == ["--permission-mode", "default"]


def test_the_config_written_alongside_names_this_participant(tmp_path):
    plan = ClaudeCodeHarness().plan_launch(
        participant_id="p-abc",
        prompt="hi",
        config_path=tmp_path / "mcp.json",
        approval="manual",
        mcp_servers=theater_mcp_servers("p-abc", "claude"),
    )
    config = json.loads(plan.files[tmp_path / "mcp.json"])
    assert config["mcpServers"]["theater"]["args"] == [
        "mcp",
        "--id",
        "p-abc",
        "--harness",
        "claude",
        "--toolset",
        "control",
    ]
    assert config["mcpServers"]["theater_wait"]["args"][-1] == "wait"


def test_a_timestamp_claude_did_not_write_leaves_the_event_unstamped(observer):
    """The observer stamps its own time; a wrong one would reorder the feed."""
    record = {
        "type": "assistant",
        "timestamp": "the day before yesterday",
        "message": {
            "id": "m1",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hi"}],
        },
    }
    (event,) = observer.parse(json.dumps(record), 0)
    assert event.ts is None
    assert event.turn_end
