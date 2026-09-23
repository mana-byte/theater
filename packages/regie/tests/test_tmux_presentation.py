from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from regie.contracts import LocalPresentationTarget, PresentationTarget
from regie.tmux.bootstrap import REGIE_PANE_OPTION, REGIE_WINDOW_OPTION
from regie.tmux.command import TmuxError
from regie.tmux.discovery import ProcessSnapshot
from regie.tmux.identity import PaneSnapshot
from regie.tmux.presentation import TmuxPresentation
from regie.tmux.session import REGIE_LAUNCH_SESSION_OPTION


def _target(
    *,
    server: str = "server-a",
    incarnation: str = "incarnation-a",
    occupant_kind: str = "tmux",
):
    return PresentationTarget(
        provider_id="provider-a",
        provider_kind="tmux",
        terminal_id="%7",
        terminal_incarnation=incarnation,
        occupant={
            "occupant_id": "participant-a",
            "provider_kind": occupant_kind,
            "tmux_server_identity": server,
            "terminal_incarnation": incarnation,
            "pane_pid": 42,
        },
    )


def _snapshot(
    *,
    incarnation: str = "incarnation-a",
    server: str = "server-a",
    pane_id: str = "%7",
    window_id: str = "@1",
) -> PaneSnapshot:
    return PaneSnapshot(
        server_identity=server,
        pane_id=pane_id,
        pane_pid=42,
        dead=False,
        executable="agent",
        window_id=window_id,
        provider_id="provider-a",
        terminal_incarnation=incarnation,
        occupant_id="participant-a",
        occupant_digest=hashlib.sha256(b"participant-a").hexdigest(),
        occupant_pane_pid=42,
        launch_id="launch-a",
        launch_executable="agent",
    )


def _patch_regie_session(monkeypatch, snapshot) -> None:
    async def session_run(*args: str, **_kwargs):
        if args[0] == "display-message":
            return "$2"
        raise AssertionError(args)

    monkeypatch.setattr("regie.tmux.session.pane_snapshot", snapshot)
    monkeypatch.setattr("regie.tmux.session.run", session_run)
    monkeypatch.setattr(
        "regie.tmux.session.current_server_identity",
        lambda: _current_server("server-a"),
    )


async def test_presentation_rechecks_identity_before_layout_mutation(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setenv("TMUX_PANE", "%99")

    async def snapshot(pane_id: str, *after: str):
        if after:
            commands.append(after)
        if pane_id == "%99":
            return _snapshot(pane_id="%99", window_id="@9")
        return _snapshot()

    async def run(*args: str, **_kwargs):
        commands.append(args)
        return ""

    monkeypatch.setattr("regie.tmux.presentation.pane_snapshot", snapshot)
    monkeypatch.setattr("regie.tmux.presentation.run", run)
    _patch_regie_session(monkeypatch, snapshot)
    presentation = TmuxPresentation(expected_server_identity="server-a")
    await presentation.stage_terminal(_target(), target_window="@9")
    assert commands == [("join-pane", "-d", "-h", "-s", "%7", "-t", "@9")]


@pytest.mark.parametrize(
    ("regie_window", "regie_server"),
    (("@8", "server-a"), ("@9", "server-b")),
)
async def test_presentation_rejects_stale_or_different_server_destination(
    monkeypatch, regie_window: str, regie_server: str
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setenv("TMUX_PANE", "%99")

    async def snapshot(pane_id: str):
        if pane_id == "%99":
            return _snapshot(pane_id="%99", window_id=regie_window, server=regie_server)
        return _snapshot()

    async def run(*args: str, **_kwargs):
        commands.append(args)
        return ""

    monkeypatch.setattr("regie.tmux.presentation.pane_snapshot", snapshot)
    monkeypatch.setattr("regie.tmux.presentation.run", run)
    _patch_regie_session(monkeypatch, snapshot)
    with pytest.raises(TmuxError):
        await TmuxPresentation(expected_server_identity="server-a").stage_terminal(
            _target(), target_window="@9"
        )
    assert commands == []


async def test_presentation_refuses_server_or_occupant_reuse(monkeypatch) -> None:
    async def replaced(_pane_id: str):
        return _snapshot(incarnation="incarnation-reused")

    monkeypatch.setattr("regie.tmux.presentation.pane_snapshot", replaced)
    presentation = TmuxPresentation(expected_server_identity="server-a")
    assert presentation.can_stage(_target(server="server-b"))[0] is False
    assert presentation.can_stage(_target(occupant_kind="other"))[0] is False
    with pytest.raises(TmuxError):
        await presentation.focus_terminal(_target())


async def test_target_window_pins_the_current_regie_server(monkeypatch) -> None:
    monkeypatch.setenv("TMUX_PANE", "%7")

    async def snapshot(_pane_id: str):
        return _snapshot()

    monkeypatch.setattr("regie.tmux.presentation.pane_snapshot", snapshot)
    _patch_regie_session(monkeypatch, snapshot)
    presentation = TmuxPresentation()
    assert await presentation.target_window() == "@1"
    assert presentation.can_stage(_target())[0] is True


async def test_presentation_session_uses_same_read_and_rejects_changed_identity(monkeypatch):
    from regie.tmux.session import TmuxPresentationSession

    monkeypatch.setenv("TMUX_PANE", "%7")
    session_id = "$2"

    async def snapshot(_pane_id):
        return replace(_snapshot(), session_id=session_id)

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("session identity must not require another tmux process")

    monkeypatch.setattr("regie.tmux.session.pane_snapshot", snapshot)
    monkeypatch.setattr("regie.tmux.session.run", unexpected)
    session = TmuxPresentationSession(expected_server_identity="server-a")
    assert await session.require_window() == "@1"
    session_id = "$3"
    with pytest.raises(TmuxError, match="identity changed"):
        await session.require_window()


@pytest.mark.parametrize("missing", [None, "window"])
async def test_target_window_fails_closed_for_missing_pane_or_window(
    monkeypatch, missing: str | None
) -> None:
    monkeypatch.setenv("TMUX_PANE", "%7")

    async def snapshot(_pane_id: str):
        if missing is None:
            return None
        pane = _snapshot()
        return PaneSnapshot(
            server_identity=pane.server_identity,
            pane_id=pane.pane_id,
            pane_pid=pane.pane_pid,
            dead=pane.dead,
            executable=pane.executable,
            window_id="",
            provider_id=pane.provider_id,
            terminal_incarnation=pane.terminal_incarnation,
            occupant_id=pane.occupant_id,
            occupant_digest=pane.occupant_digest,
            occupant_pane_pid=pane.occupant_pane_pid,
            launch_id=pane.launch_id,
            launch_executable=pane.launch_executable,
        )

    monkeypatch.setattr("regie.tmux.presentation.pane_snapshot", snapshot)
    _patch_regie_session(monkeypatch, snapshot)
    with pytest.raises(TmuxError):
        await TmuxPresentation(expected_server_identity="server-a").target_window()


async def test_target_window_rejects_wrong_server_identity(monkeypatch) -> None:
    monkeypatch.setenv("TMUX_PANE", "%7")

    async def snapshot(_pane_id: str):
        return _snapshot()

    monkeypatch.setattr("regie.tmux.presentation.pane_snapshot", snapshot)
    _patch_regie_session(monkeypatch, snapshot)
    with pytest.raises(TmuxError):
        await TmuxPresentation(expected_server_identity="server-b").target_window()


async def test_unmanaged_discovery_excludes_shells_self_dead_and_any_provider_identity(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TMUX_PANE", "%99")
    current = replace(
        _snapshot(pane_id="%99", window_id="@9"),
        provider_id=None,
        terminal_incarnation=None,
        occupant_id=None,
        occupant_digest=None,
        occupant_pane_pid=None,
        launch_id=None,
        launch_executable=None,
    )
    unmanaged = replace(
        current,
        pane_id="%8",
        pane_pid=80,
        executable="zsh",
        cwd="/workspace/project",
        session_name="work",
        window_name="shell",
    )
    dead = replace(unmanaged, pane_id="%10", dead=True)
    partly_marked = replace(unmanaged, pane_id="%11", provider_id="provider-a")
    ordinary_shell = replace(unmanaged, pane_id="%12", pane_pid=120)

    async def snapshot(_pane_id: str):
        return current

    async def inventory() -> tuple[PaneSnapshot, ...]:
        return current, unmanaged, dead, partly_marked, ordinary_shell

    processes = ProcessSnapshot(
        children={80: ((81, "/nix/store/hash/bin/.opencode-wrapp"),)},
        commands={80: "/bin/zsh", 120: "/bin/zsh"},
    )

    monkeypatch.setattr("regie.tmux.presentation.pane_inventory", inventory)
    monkeypatch.setattr("regie.tmux.presentation.capture_process_snapshot", lambda: processes)
    _patch_regie_session(monkeypatch, snapshot)

    rows = await TmuxPresentation(expected_server_identity="server-a").unmanaged_panes(
        harness_commands={"opencode": ("opencode", ".opencode-wrapped")}
    )

    assert len(rows) == 1
    assert rows[0].pane_id == "%8"
    assert rows[0].command == "zsh"
    assert rows[0].cwd == "/workspace/project"
    assert rows[0].harness == "opencode"


async def test_unmanaged_stage_is_fenced_to_the_discovered_server_and_pane_process(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TMUX_PANE", "%99")
    current = replace(
        _snapshot(pane_id="%99", window_id="@9"),
        provider_id=None,
        terminal_incarnation=None,
        occupant_id=None,
        occupant_digest=None,
        occupant_pane_pid=None,
        launch_id=None,
        launch_executable=None,
    )
    unmanaged = replace(
        current,
        pane_id="%8",
        pane_pid=80,
        executable="codex",
        cwd="/workspace/project",
    )
    pane_reused = False
    commands: list[tuple[str, ...]] = []

    async def snapshot(pane_id: str, *after: str):
        if after:
            commands.append(after)
        if pane_id == "%99":
            return current
        if pane_id == "%8":
            return replace(unmanaged, pane_pid=81) if pane_reused else unmanaged
        return None

    async def inventory() -> tuple[PaneSnapshot, ...]:
        return current, unmanaged

    async def presentation_run(*args: str, **_kwargs):
        commands.append(args)
        return ""

    monkeypatch.setattr("regie.tmux.presentation.pane_inventory", inventory)
    monkeypatch.setattr(
        "regie.tmux.presentation.capture_process_snapshot",
        lambda: ProcessSnapshot(children={}, commands={80: "codex"}),
    )
    monkeypatch.setattr("regie.tmux.presentation.pane_snapshot", snapshot)
    monkeypatch.setattr("regie.tmux.presentation.run", presentation_run)
    _patch_regie_session(monkeypatch, snapshot)
    presentation = TmuxPresentation(expected_server_identity="server-a")

    rows = await presentation.unmanaged_panes(harness_commands={"codex": ("codex",)})
    assert [row.pane_id for row in rows] == ["%8"]
    target = LocalPresentationTarget("%8")
    await presentation.stage_terminal(target, target_window="@9")
    assert commands == [("join-pane", "-d", "-h", "-s", "%8", "-t", "@9")]

    assert await presentation.unmanaged_panes(harness_commands={}) == ()
    pane_reused = True
    with pytest.raises(TmuxError, match="no longer matches"):
        await presentation.focus_terminal(target)
    assert len(commands) == 1


async def test_session_presentation_restores_options_binding_and_sidebar(monkeypatch) -> None:
    options: dict[str, str] = {"mouse": "off"}
    binding_note: str | None = None
    commands: list[tuple[str, ...]] = []
    pane_available = True
    monkeypatch.setenv("TMUX_PANE", "%99")

    async def snapshot(_pane_id: str, *after: str):
        if after:
            commands.append(after)
        return _snapshot(pane_id="%99", window_id="@9") if pane_available else None

    async def session_run(*args: str, **_kwargs):
        nonlocal binding_note
        commands.append(args)
        if args[0] == "display-message":
            return "$2"
        if args[0] == "show-options":
            name = args[-1]
            return f"{name} {options[name]}" if name in options else ""
        if args[0] == "set-option":
            if args[1] == "-u":
                options.pop(args[-1], None)
            else:
                options[args[-2]] = args[-1]
            return ""
        if args[0] == "list-keys":
            if args[-1] == "#{key_string}":
                return "" if binding_note is None else "h"
            return "" if binding_note is None else f"h\t{binding_note}"
        if args[0] == "bind-key":
            binding_note = args[args.index("-N") + 1]
            assert "send-keys -t %99 C-g" in args
            return ""
        if args[0] == "unbind-key":
            binding_note = None
            return ""
        if args[0] == "resize-pane":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr("regie.tmux.session.pane_snapshot", snapshot)
    monkeypatch.setattr("regie.tmux.session.run", session_run)
    monkeypatch.setattr(
        "regie.tmux.session.current_server_identity",
        lambda: _current_server("server-a"),
    )
    presentation = TmuxPresentation(expected_server_identity="server-a")

    await presentation.open()
    assert options == {
        "mouse": "on",
        "status": "off",
        REGIE_WINDOW_OPTION: "1",
        REGIE_PANE_OPTION: "%99",
        REGIE_LAUNCH_SESSION_OPTION: "$2",
    }
    assert binding_note == "theater-regie-return:%99"
    await presentation.resize_regie(width=52)
    pane_available = False
    await presentation.close()
    command_count = len(commands)
    await presentation.close()

    assert options == {"mouse": "off"}
    assert binding_note is None
    assert ("resize-pane", "-t", "%99", "-x", "52") in commands
    assert len(commands) == command_count


async def test_session_teardown_does_not_restore_into_a_replaced_server(monkeypatch) -> None:
    options: dict[str, str] = {"mouse": "off"}
    pane_available = True
    server_identity = "server-a"
    monkeypatch.setenv("TMUX_PANE", "%99")

    async def snapshot(_pane_id: str):
        return _snapshot(pane_id="%99", window_id="@9") if pane_available else None

    async def session_run(*args: str, **_kwargs):
        if args[0] == "display-message":
            return "$2"
        if args[0] == "show-options":
            name = args[-1]
            return f"{name} {options[name]}" if name in options else ""
        if args[0] == "set-option":
            if args[1] == "-u":
                options.pop(args[-1], None)
            else:
                options[args[-2]] = args[-1]
            return ""
        if args[0] == "list-keys":
            return "h"
        raise AssertionError(args)

    async def current_server() -> str:
        return server_identity

    monkeypatch.setattr("regie.tmux.session.pane_snapshot", snapshot)
    monkeypatch.setattr("regie.tmux.session.run", session_run)
    monkeypatch.setattr("regie.tmux.session.current_server_identity", current_server)
    presentation = TmuxPresentation(expected_server_identity="server-a")

    await presentation.open()
    assert options["mouse"] == "on"
    pane_available = False
    server_identity = "server-b"
    before_close = dict(options)

    await presentation.close()

    assert options == before_close


async def _current_server(identity: str) -> str:
    return identity
