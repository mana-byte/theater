from __future__ import annotations

import hashlib

import pytest
from regie.contracts import PresentationTarget
from regie.tmux.command import TmuxError
from regie.tmux.identity import PaneSnapshot
from regie.tmux.presentation import TmuxPresentation


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


async def test_presentation_rechecks_identity_before_layout_mutation(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setenv("TMUX_PANE", "%99")

    async def snapshot(pane_id: str):
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


async def test_session_presentation_restores_options_binding_and_sidebar(monkeypatch) -> None:
    options: dict[str, str] = {"mouse": "off"}
    binding_note: str | None = None
    commands: list[tuple[str, ...]] = []
    monkeypatch.setenv("TMUX_PANE", "%99")

    async def snapshot(_pane_id: str):
        return _snapshot(pane_id="%99", window_id="@9")

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
    presentation = TmuxPresentation(expected_server_identity="server-a")

    await presentation.open()
    assert options == {"mouse": "on", "status": "off"}
    assert binding_note == "theater-regie-return:%99"
    await presentation.resize_regie(width=52)
    await presentation.close()
    command_count = len(commands)
    await presentation.close()

    assert options == {"mouse": "off"}
    assert binding_note is None
    assert ("resize-pane", "-t", "%99", "-x", "52") in commands
    assert len(commands) == command_count
