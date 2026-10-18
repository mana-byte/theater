"""Parser surface, hidden hook commands, `main()` dispatch and error mapping."""

from __future__ import annotations

import argparse
import ast
import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.cli_fakes import parse
from theater import cli
from theater.cli.commands import identity
from theater.cli.commands import launch as launch_mod
from theater.cli.commands import participants as participants_mod
from theater.protocol import RemoteError

EXPECTED_COMMANDS = {
    "adopt",
    "bind",
    "bus",
    "candidates",
    "claude-receipt",
    "config",
    "control-transfer",
    "controls",
    "daemon",
    "gc",
    "harness-event",
    "harnesses",
    "interrupt",
    "kill",
    "ls",
    "mcp",
    "models",
    "name",
    "plugin",
    "plugins",
    "providers",
    "queue",
    "regie",
    "reload",
    "restart",
    "settings",
    "skills",
    "spawn",
    "stats",
    "steer",
    "stop",
    "transcript-receipt",
    "workspaces",
}


HIDDEN_HOOK_ARGS = (
    (
        "transcript-receipt",
        "--id",
        "p-abc123",
        "--token-file",
        "/tmp/token",
        "--strict-exit",
    ),
    ("claude-receipt", "--id", "p-abc123", "--token-file", "/tmp/token"),
    (
        "harness-event",
        "tool.finished",
        "--id",
        "p-abc123",
        "--channel",
        "hooks",
        "--token-file",
        "/tmp/token",
        "--delivery-id",
        "delivery-1",
        "--strict-exit",
    ),
)


def test_the_cli_exposes_exactly_these_subcommands():
    """Use a cold subprocess if command registration becomes import-driven."""
    parser = cli._parser()
    (subcommands,) = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    assert set(subcommands.choices) == EXPECTED_COMMANDS


def test_hidden_hook_commands_are_absent_from_top_level_help(capsys):
    with pytest.raises(SystemExit):
        parse("--help")
    out = capsys.readouterr().out
    assert "==SUPPRESS==" not in out
    for command, *_ in HIDDEN_HOOK_ARGS:
        assert command not in out


def test_top_level_help_is_grouped_and_hides_retired_commands():
    from theater.cli.help import COMMAND_GROUPS

    out = cli._parser().format_help()
    usage = out.splitlines()[0]
    assert "<command> ..." in usage
    assert "{" not in usage
    for title, subgroups in COMMAND_GROUPS:
        assert f"{title}:" in out
        for sub_title, names in subgroups:
            if sub_title is not None:
                assert f"  {sub_title}:" in out
            for name in names:
                assert f"  {name} " in out
    assert "adopt" not in out
    assert "Moved to the standalone" not in out


@pytest.mark.parametrize("argv", HIDDEN_HOOK_ARGS)
def test_hidden_hook_commands_remain_parseable_and_dispatchable(monkeypatch, argv):
    command = argv[0]
    parsed = parse(*argv)
    assert parsed.command == command
    assert command in cli._COMMANDS

    dispatched = []

    def fake_command(args):
        dispatched.append(args)
        return 17

    monkeypatch.setitem(cli._COMMANDS, command, fake_command)
    monkeypatch.setattr(cli.paths, "ensure_home", lambda: None)
    monkeypatch.setattr(cli.config, "load", object)
    monkeypatch.setattr(cli.harness_registry, "install", lambda _: None)

    assert cli.main(list(argv)) == 17
    assert len(dispatched) == 1
    assert vars(dispatched[0]) == vars(parsed)


@pytest.mark.parametrize("flag", ["--version", "-v"])
def test_version_flag_prints_the_version_and_exits_clean(flag, capsys):
    """Both spellings print `theater <version>` and exit before dispatch."""
    from theater import __version__

    with pytest.raises(SystemExit) as exc:
        parse(flag)
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"theater {__version__}"


def test_a_bare_invocation_selects_the_default_launcher():
    assert parse().command is None


def test_legacy_regie_command_gives_the_standalone_migration_without_home_setup(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        cli.paths,
        "ensure_home",
        lambda: (_ for _ in ()).throw(
            AssertionError("legacy UI invocation must not start Theater")
        ),
    )
    assert cli.main(["regie"]) == 1
    message = capsys.readouterr().err
    assert "standalone `regie`" in message
    assert "regie bridge start" in message


def test_bare_cli_prints_help_and_the_standalone_regie_guidance(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.paths,
        "ensure_home",
        lambda: (_ for _ in ()).throw(AssertionError("help must not start Theater")),
    )
    monkeypatch.setitem(
        cli._COMMANDS,
        None,
        lambda _args: (_ for _ in ()).throw(AssertionError("legacy launcher was dispatched")),
    )
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert "usage: theater" in output
    assert "standalone `regie`" in output


def test_theater_cli_never_imports_textual_or_the_retired_regie_ui() -> None:
    violations: list[str] = []
    source_root = Path(cli.__file__).parent
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module is not None
                else []
            )
            if any(
                name == "textual" or name.startswith(("textual.", "theater.regie"))
                for name in names
            ):
                violations.append(str(path.relative_to(source_root)))

    assert violations == []


def test_mcp_serves_the_id_it_was_given(monkeypatch):
    import theater.mcp.server as server_mod
    from theater.observability import runtime as runtime_mod

    class _FakeHandle:
        def shutdown(self) -> None:
            pass

    monkeypatch.setattr(runtime_mod, "configure", lambda **kw: _FakeHandle())
    seen: list = []
    monkeypatch.setattr(
        server_mod,
        "main",
        lambda pid, harness, toolset: seen.append((pid, harness, toolset)),
    )
    assert cli.cmd_mcp(parse("mcp", "--id", "p-abc", "--harness", "vibe")) == 0
    assert seen == [("p-abc", "vibe", "all")]


def test_mcp_accepts_the_internal_toolset_switch():
    assert parse("mcp", "--toolset", "wait").toolset == "wait"


def test_main_dispatches_to_the_named_command(monkeypatch, capsys):

    monkeypatch.setattr(participants_mod, "call_sync", lambda m, **p: [])
    assert cli.main(["ls"]) == 0


def test_main_turns_a_remote_error_into_one_line(monkeypatch, capsys):
    def fail(method, **params):
        raise RemoteError("busy", "target is mid-turn")

    monkeypatch.setattr(participants_mod, "call_sync", fail)
    assert cli.main(["kill", "p-abc"]) == 1
    assert "busy: target is mid-turn" in capsys.readouterr().err


def test_main_turns_an_unreachable_daemon_into_one_line(monkeypatch, capsys):
    def fail(method, **params):
        raise ConnectionError("no daemon at /tmp/theater.sock")

    monkeypatch.setattr(participants_mod, "call_sync", fail)
    assert cli.main(["kill", "p-abc"]) == 1
    assert "no daemon" in capsys.readouterr().err


def test_ctrl_c_out_of_a_follow_is_not_a_crash(monkeypatch):
    """`bus -f` and `ls --watch` are ended this way; 130 is what a shell expects."""

    def interrupt(method, **params):
        raise KeyboardInterrupt

    monkeypatch.setattr(participants_mod, "call_sync", interrupt)
    assert cli.main(["kill", "p-abc"]) == 130


def test_legacy_launch_helper_only_gives_standalone_guidance(capsys):
    assert launch_mod.cmd_launch(object()) == 0
    assert "standalone `regie`" in capsys.readouterr().out


def _args(token_file, *, strict: bool = False):
    return SimpleNamespace(
        id="participant",
        channel="native-hooks",
        event="tool.finished",
        delivery_id="delivery-1",
        token_file=str(token_file),
        strict_exit=strict,
    )


def test_harness_event_forwards_opaque_object_without_autostart(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    seen = {}

    async def send(args, *, token, payload) -> None:
        seen["args"] = args
        seen["token"] = token
        seen["payload"] = payload

    monkeypatch.setattr(identity, "_send_harness_event", send)
    monkeypatch.setattr(identity.sys, "stdin", io.StringIO('{"native_id":"n-1"}'))

    assert identity.cmd_harness_event(_args(token_file)) == 0
    assert seen["token"] == "secret"
    assert seen["payload"] == {"native_id": "n-1"}


def test_harness_event_sender_disables_daemon_autostart(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    seen = {}

    class Client:
        def __init__(self, *, autostart: bool) -> None:
            seen["autostart"] = autostart

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def call(self, method, **params) -> None:
            seen["method"] = method
            seen["params"] = params

    monkeypatch.setattr(identity, "DaemonClient", Client)
    asyncio.run(
        identity._send_harness_event(
            _args(token_file), token="secret", payload={"native_id": "n-1"}
        )
    )
    assert seen["autostart"] is False
    assert seen["method"] == "harness.event"


def test_harness_event_rejects_invalid_utf8_quietly_or_strictly(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    invalid = io.TextIOWrapper(io.BytesIO(b"\xff"), encoding="utf-8")
    monkeypatch.setattr(identity.sys, "stdin", invalid)

    assert identity.cmd_harness_event(_args(token_file)) == 0
    invalid = io.TextIOWrapper(io.BytesIO(b"\xff"), encoding="utf-8")
    monkeypatch.setattr(identity.sys, "stdin", invalid)
    assert identity.cmd_harness_event(_args(token_file, strict=True)) == 1


def test_harness_event_rejects_oversized_token_file(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("x" * 128 + "\nextra")
    monkeypatch.setattr(identity.sys, "stdin", io.StringIO('{"native_id":"n-1"}'))
    assert identity.cmd_harness_event(_args(token_file)) == 0
    monkeypatch.setattr(identity.sys, "stdin", io.StringIO('{"native_id":"n-1"}'))
    assert identity.cmd_harness_event(_args(token_file, strict=True)) == 1


def _deep_payload() -> str:
    payload: dict[str, object] = {}
    for _ in range(10):
        payload = {"next": payload}
    return json.dumps(payload)


@pytest.mark.parametrize(
    "payload",
    (
        "not json",
        "[]",
        _deep_payload(),
        json.dumps({f"key-{index}": index for index in range(200)}),
    ),
)
def test_harness_event_rejects_malformed_or_unbounded_payloads(
    monkeypatch, tmp_path, payload
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    monkeypatch.setattr(identity.sys, "stdin", io.StringIO(payload))
    assert identity.cmd_harness_event(_args(token_file)) == 0
    monkeypatch.setattr(identity.sys, "stdin", io.StringIO(payload))
    assert identity.cmd_harness_event(_args(token_file, strict=True)) == 1
