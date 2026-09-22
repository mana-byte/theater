from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from regie.bridge.runtime import TmuxBridge
from regie.contracts import BridgeConfig, PresentationTarget
from regie.tmux import bootstrap, terminals
from regie.tmux.bootstrap import REGIE_DEFAULT_SESSION
from regie.tmux.command import TmuxError, available, run, sequence_argv
from regie.tmux.identity import pane_snapshot
from regie.tmux.presentation import TmuxPresentation
from regie.tmux.session import REGIE_LAUNCH_SESSION_OPTION
from regie.tmux.terminals import (
    create_terminal,
    deliver_action,
    ensure_server,
    inspect_terminal,
    managed_inventory,
    terminate_terminal,
)

pytestmark = pytest.mark.tmux


async def test_provider_launch_prefers_regie_pinned_session(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args: str, **_kwargs) -> str:
        calls.append(args)
        if args[0] == "show-options":
            assert args[-1] == REGIE_LAUNCH_SESSION_OPTION
            return "$7"
        if args[0] == "list-panes":
            return "$2\t%2\t0\t\t\n$7\t%7\t0\t1\t%7"
        raise AssertionError(args)

    monkeypatch.setattr(terminals, "run", fake_run)

    assert await terminals._launch_session() == "$7"
    assert calls[-1][0] == "list-panes"


async def test_provider_launch_ignores_stale_regie_pin_and_prefers_default(monkeypatch) -> None:
    async def fake_run(*args: str, **_kwargs) -> str:
        if args[0] == "show-options":
            return "$7"
        if args[0] == "list-panes":
            return "$7\t%8\t0\t1\t%7\n$2\t%2\t0\t\t"
        if args[0] == "list-sessions":
            return f"zeta\n{REGIE_DEFAULT_SESSION}\nalpha"
        raise AssertionError(args)

    monkeypatch.setattr(terminals, "run", fake_run)

    assert await terminals._launch_session() == REGIE_DEFAULT_SESSION


async def test_provider_launch_creates_default_session_when_unpinned(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args: str, **_kwargs) -> str:
        calls.append(args)
        if args[0] == "show-options":
            return ""
        if args[0] == "list-sessions":
            return "work"
        if args[0] == "new-session":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(terminals, "run", fake_run)

    assert await terminals._launch_session(cwd="/project") == REGIE_DEFAULT_SESSION
    assert calls[-1] == (
        "new-session",
        "-d",
        "-s",
        REGIE_DEFAULT_SESSION,
        "-c",
        "/project",
    )


@pytest.fixture
async def isolated_tmux(tmp_path: Path, monkeypatch):
    if not available():
        pytest.skip("tmux is not on PATH")
    socket_root = Path(tempfile.mkdtemp(prefix="regie-tmux-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_root))
    monkeypatch.setenv("TERM", "xterm-256color")
    for name in ("TMUX", "TMUX_PANE", "THEATER_ID"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield tmp_path
    finally:
        await run("kill-server", check=False)
        shutil.rmtree(socket_root)


async def test_command_sequence_preserves_literals_and_stops_at_first_error(isolated_tmux):
    await ensure_server(cwd=str(isolated_tmux))
    values = ("", ";", "x;", "x\\;", "x\\\\;", "a;b", "spaces 'quotes' $()", "two\nlines")
    commands = [
        ("set-environment", "-g", f"REGIE_TEST_{i}", value) for i, value in enumerate(values)
    ]

    await run(*sequence_argv(commands))

    for i, value in enumerate(values):
        assert await run("show-environment", "-g", f"REGIE_TEST_{i}") == f"REGIE_TEST_{i}={value}"
    with pytest.raises(TmuxError):
        await run(
            *sequence_argv(
                [
                    ("set-environment", "-g", "REGIE_BEFORE", "yes"),
                    ("set-option", "-g", "not-a-tmux-option", "bad"),
                    ("set-environment", "-g", "REGIE_AFTER", "no"),
                ]
            )
        )
    assert await run("show-environment", "-g", "REGIE_BEFORE") == "REGIE_BEFORE=yes"
    with pytest.raises(TmuxError):
        await run("show-environment", "-g", "REGIE_AFTER")
    for empty in ([], [()]):
        with pytest.raises(ValueError):
            sequence_argv(empty)


async def test_focus_hooks_preserve_user_hooks_wake_and_close_on_isolated_server(isolated_tmux):
    from regie.tmux.focus_facts import read_inventory
    from regie.tmux.focus_hooks import FocusHooks

    server = await ensure_server(cwd=str(isolated_tmux))
    hooks = FocusHooks(server)
    window = await run("display-message", "-p", "-t", REGIE_DEFAULT_SESSION, "#{window_id}")
    user_hooks = (
        (("-g",), "after-select-pane[0]", "global"),
        (("-t", REGIE_DEFAULT_SESSION), "after-select-pane[3]", "local"),
        (("-g", "-w"), "pane-focus-in[0]", "window-global"),
        (("-w", "-t", window), "pane-focus-out[3]", "window-local"),
    )
    await run("set-option", "-g", "focus-events", "off")
    for scope, entry, value in user_hooks:
        await run("set-hook", *scope, entry, f"set-option -g @user-hook {value}")
    assert not await hooks.arm()
    first = [await run("show-hooks", *scope) for scope, _, _ in user_hooks]
    assert await hooks.arm()
    assert [await run("show-hooks", *scope) for scope, _, _ in user_hooks] == first
    for output, (_, entry, value) in zip(first, user_hooks, strict=True):
        assert f"{entry} set-option -g @user-hook {value}" in output
        assert hooks.command in output
    assert (await read_inventory(server)).enabled
    waiter = asyncio.create_task(hooks.wait())
    try:
        await run("split-window", "-d", "-t", REGIE_DEFAULT_SESSION)
        await asyncio.wait_for(waiter, 2)
        await run("set-hook", "-R", "-t", REGIE_DEFAULT_SESSION, "after-select-pane")
        assert await run("show-options", "-g", "-v", "@user-hook") == "local"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await hooks.close()
    for scope, entry, value in user_hooks:
        output = await run("show-hooks", *scope)
        assert f"{entry} set-option -g @user-hook {value}" in output
        assert hooks.command not in output
    assert await run("show-options", "-g", "-v", "focus-events") == "on"


async def _assert_presentation_lifecycle(
    presentation: TmuxPresentation,
    target: PresentationTarget,
    *,
    stage_window: str,
    regie_pane: str,
) -> None:
    mouse_before = await run("show-options", "-t", REGIE_DEFAULT_SESSION, "mouse")
    status_before = await run("show-options", "-t", REGIE_DEFAULT_SESSION, "status")
    await presentation.open()
    assert await run("show-options", "-v", "-t", REGIE_DEFAULT_SESSION, "mouse") == "on"
    assert await run("show-options", "-v", "-t", REGIE_DEFAULT_SESSION, "status") == "off"
    await presentation.stage_terminal(target, target_window=stage_window)
    await presentation.resize_regie(width=52)
    assert await run("display-message", "-p", "-t", regie_pane, "#{pane_width}") == "52"
    assert await presentation.terminal_exists(target)
    await presentation.focus_terminal(target)
    assert (
        await run("display-message", "-p", "-t", stage_window, "#{pane_id}") == target.terminal_id
    )
    await presentation.unstage_terminal(target)
    assert await run("display-message", "-p", "-t", stage_window, "#{pane_id}") == regie_pane
    assert await presentation.terminal_exists(target)
    await presentation.close()
    assert await run("show-options", "-t", REGIE_DEFAULT_SESSION, "mouse") == mouse_before
    assert await run("show-options", "-t", REGIE_DEFAULT_SESSION, "status") == status_before


async def test_real_tmux_provider_preserves_identity_delivery_and_presentation(
    isolated_tmux: Path,
    monkeypatch,
) -> None:
    result_path = isolated_tmux / "input.txt"
    shell_marker = isolated_tmux / "shell-interpreted"
    server = await ensure_server(cwd=str(isolated_tmux))
    program = (
        "import json,os,sys,time; value=input(); "
        "json.dump({'argv':sys.argv[1:],'environment':os.environ['EXACT_ENV'],'input':value},"
        "open(os.environ['RESULT_PATH'],'w',encoding='utf-8')); time.sleep(30)"
    )
    exact_arguments = ["space value", f"$(touch {shell_marker})", "semi;colon", "'quoted'"]
    identity = await create_terminal(
        provider_id="provider-a",
        generation=2,
        participant_id="participant-a",
        launch_id="launch-a",
        executable=sys.executable,
        argv=[sys.executable, "-c", program, *exact_arguments],
        cwd=str(isolated_tmux),
        environment={"RESULT_PATH": str(result_path), "EXACT_ENV": "space ; $(literal)"},
        presentation={"name": "provider-test", "background": True},
        expected_server_identity=server,
        terminal_incarnation="tmux-test-incarnation",
        provisional_window_name="regie-launch-testtoken123",
        dispatch_previously_started=False,
    )
    terminal_id = str(identity["terminal_id"])
    incarnation = str(identity["terminal_incarnation"])
    occupant = identity["occupant"]
    assert isinstance(occupant, dict)

    inventory = await managed_inventory(
        provider_id="provider-a", generation=2, expected_server_identity=server
    )
    assert inventory == (identity,)
    inspected, presence, _screen, alive = await inspect_terminal(
        provider_id="provider-a",
        generation=2,
        terminal_id=terminal_id,
        terminal_incarnation=incarnation,
        expected_server_identity=server,
    )
    assert inspected == identity
    assert presence.state == "absent"
    assert alive is True

    await run("copy-mode", "-t", terminal_id)
    _identity_value, copy_presence, _screen, _alive = await inspect_terminal(
        provider_id="provider-a",
        generation=2,
        terminal_id=terminal_id,
        terminal_incarnation=incarnation,
        expected_server_identity=server,
    )
    assert copy_presence.mode == "copy"
    await run("send-keys", "-t", terminal_id, "-X", "cancel")

    await deliver_action(terminal_id, {"kind": "submit_text", "text": "literal! ~input"})
    async with asyncio.timeout(5):
        while not result_path.exists():  # noqa: ASYNC110
            await asyncio.sleep(0.02)
    result = json.loads(result_path.read_text())
    assert result == {
        "argv": exact_arguments,
        "environment": "space ; $(literal)",
        "input": "literal! ~input",
    }
    assert not shell_marker.exists()

    reclaimed = await create_terminal(
        provider_id="provider-a",
        generation=3,
        participant_id="participant-a",
        launch_id="launch-a",
        executable=sys.executable,
        argv=[sys.executable, "-c", program, *exact_arguments],
        cwd=str(isolated_tmux),
        environment={"RESULT_PATH": str(result_path), "EXACT_ENV": "space ; $(literal)"},
        presentation={"name": "provider-test", "background": True},
        expected_server_identity=server,
        terminal_incarnation="ignored-on-reclaim",
        provisional_window_name="regie-launch-anothertoken123",
        dispatch_previously_started=False,
    )
    assert reclaimed["terminal_id"] == terminal_id
    assert reclaimed["provider_generation"] == 3
    assert (
        len(
            await managed_inventory(
                provider_id="provider-a", generation=3, expected_server_identity=server
            )
        )
        == 1
    )

    bridge = TmuxBridge(
        BridgeConfig(
            theater_socket=isolated_tmux / "unused.sock",
            state_dir=isolated_tmux / "bridge-state",
        )
    )
    await bridge.close()
    _identity_value, _presence, _screen, alive_after_stop = await inspect_terminal(
        provider_id="provider-a",
        generation=3,
        terminal_id=terminal_id,
        terminal_incarnation=incarnation,
        expected_server_identity=server,
    )
    assert alive_after_stop is True

    target = PresentationTarget(
        provider_id="provider-a",
        provider_kind="tmux",
        terminal_id=terminal_id,
        terminal_incarnation=incarnation,
        occupant=occupant,
    )
    stage_window = await run(
        "display-message", "-p", "-t", f"{REGIE_DEFAULT_SESSION}:", "#{window_id}"
    )
    regie_pane = await run("display-message", "-p", "-t", f"{REGIE_DEFAULT_SESSION}:", "#{pane_id}")
    monkeypatch.setenv("TMUX_PANE", regie_pane)
    presentation = TmuxPresentation(expected_server_identity=server)
    await _assert_presentation_lifecycle(
        presentation,
        target,
        stage_window=stage_window,
        regie_pane=regie_pane,
    )

    snapshot = await pane_snapshot(terminal_id)
    assert snapshot is not None
    assert await terminate_terminal(snapshot)
    assert not await presentation.terminal_exists(target)
    assert shutil.which("tmux") is not None
    assert "THEATER_ID" not in os.environ


async def test_regie_window_is_created_and_reused_on_the_pinned_server(
    isolated_tmux: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    server = await ensure_server(cwd=str(isolated_tmux))
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("COLORTERM", "truecolor")
    command = (sys.executable, "-c", "import time; time.sleep(30)")

    first = await bootstrap.ensure_regie_window(
        str(isolated_tmux),
        command=command,
        expected_server_identity=server,
    )
    second = await bootstrap.ensure_regie_window(
        str(isolated_tmux),
        command=command,
        expected_server_identity=server,
    )

    assert second == first
    socket_path, session, window = first
    assert socket_path
    assert session == REGIE_DEFAULT_SESSION
    assert window.startswith("@")
    server_environment = (await run("show-environment", "-g")).splitlines()
    assert "COLORTERM=truecolor" in server_environment
    assert not any(line.startswith("NO_COLOR=") for line in server_environment)
    assert (
        await run(
            "show-options",
            "-w",
            "-v",
            "-t",
            window,
            bootstrap.REGIE_WINDOW_OPTION,
        )
        == bootstrap.REGIE_WINDOW_OPTION_VALUE
    )
    assert await run(
        "show-options",
        "-w",
        "-v",
        "-t",
        window,
        bootstrap.REGIE_PANE_OPTION,
    ) == await run("display-message", "-p", "-t", window, "#{pane_id}")


@pytest.mark.parametrize("generation", [1, 2])
async def test_human_ctrl_c_proves_terminal_exit(isolated_tmux, generation):
    """A real process exit stays observable across bridge reconnection."""
    from regie.bridge.callbacks import TmuxProviderCallbacks
    from regie.bridge.state import BridgeStateStore

    from theater.frontend import CallbackRequest, CallbackResponse
    from theater.frontend.schemas import validate_callback_request, validate_callback_response

    state = BridgeStateStore(isolated_tmux / "bridge")
    state.acquire()
    try:
        server = await ensure_server(cwd=str(isolated_tmux))
        state.update(provider_id="provider-a", tmux_server_identity=server)
        callbacks = TmuxProviderCallbacks(
            state, generation_usable=lambda value: value == generation
        )
        identity = await create_terminal(
            provider_id="provider-a",
            generation=1,
            participant_id="participant-a",
            launch_id="launch-a",
            executable=sys.executable,
            argv=[sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
            cwd=str(isolated_tmux),
            environment={},
            presentation=None,
            expected_server_identity=server,
            terminal_incarnation="incarnation-a",
            provisional_window_name="regie-launch-ctrlctest123",
            dispatch_previously_started=False,
        )
        terminal_id = identity["terminal_id"]
        async with asyncio.timeout(5):
            while "ready" not in await run("capture-pane", "-p", "-t", terminal_id):  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        await run("send-keys", "-t", terminal_id, "C-c")
        async with asyncio.timeout(5):
            while await pane_snapshot(terminal_id) is not None:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        expected = {**identity, "provider_generation": generation}
        params = {
            "provider_generation": generation,
            "terminal_id": terminal_id,
            "terminal_incarnation": "incarnation-a",
            "expected_terminal": expected,
        }
        method = "terminal.inspect"
        validate_callback_request(
            {"type": "request", "id": "cb", "method": method, "params": params}
        )
        result = await callbacks.inspect(
            CallbackRequest(
                callback_id="cb", method=method, params=params, provider_generation=generation
            )
        )
        assert not isinstance(result, CallbackResponse), result
        validate_callback_response(method, {"type": "response", "id": "cb", "result": result})
        assert result["terminal"] == expected
        assert result["lifecycle"] == {
            "alive": False,
            "authoritative": True,
            "reason": "terminal_missing",
        }
    finally:
        state.release()


async def test_dead_regie_pane_is_not_reused_when_a_staged_pane_keeps_its_window_alive(
    isolated_tmux: Path,
) -> None:
    server = await ensure_server(cwd=str(isolated_tmux))
    created = await run(
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id}\t#{pane_id}",
        "-t",
        f"{REGIE_DEFAULT_SESSION}:",
        "-n",
        "stale-regie",
        "--",
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
    )
    stale_window, stale_pane = created.split("\t")
    await run(
        "set-option",
        "-w",
        "-t",
        stale_window,
        bootstrap.REGIE_WINDOW_OPTION,
        bootstrap.REGIE_WINDOW_OPTION_VALUE,
    )
    await run(
        "set-option",
        "-w",
        "-t",
        stale_window,
        bootstrap.REGIE_PANE_OPTION,
        stale_pane,
    )
    survivor = await run(
        "split-window",
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        stale_window,
        "--",
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
    )
    await run("kill-pane", "-t", stale_pane)

    _socket, _session, replacement_window = await bootstrap.ensure_regie_window(
        str(isolated_tmux),
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
        expected_server_identity=server,
    )

    assert replacement_window != stale_window
    assert await run("display-message", "-p", "-t", survivor, "#{window_id}") == stale_window


async def test_server_and_pane_id_reuse_cannot_satisfy_an_old_identity(
    isolated_tmux: Path,
) -> None:
    server = await ensure_server(cwd=str(isolated_tmux))
    old = await create_terminal(
        provider_id="provider-a",
        generation=2,
        participant_id="participant-a",
        launch_id="launch-a",
        executable=sys.executable,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(isolated_tmux),
        environment={},
        presentation={"background": True},
        expected_server_identity=server,
        terminal_incarnation="tmux-original-incarnation",
        provisional_window_name="regie-launch-originaltoken123",
        dispatch_previously_started=False,
    )
    old_target = PresentationTarget(
        provider_id="provider-a",
        provider_kind="tmux",
        terminal_id=str(old["terminal_id"]),
        terminal_incarnation=str(old["terminal_incarnation"]),
        occupant=old["occupant"],
    )
    presentation = TmuxPresentation(expected_server_identity=server)

    await run("kill-server")
    replacement_server = await ensure_server(cwd=str(isolated_tmux))
    replacement = await create_terminal(
        provider_id="provider-a",
        generation=3,
        participant_id="participant-b",
        launch_id="launch-b",
        executable=sys.executable,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(isolated_tmux),
        environment={},
        presentation={"background": True},
        expected_server_identity=replacement_server,
        terminal_incarnation="tmux-replacement-incarnation",
        provisional_window_name="regie-launch-replacement123",
        dispatch_previously_started=False,
    )
    assert replacement_server != server
    assert replacement["terminal_id"] == old["terminal_id"]
    assert not await presentation.terminal_exists(old_target)
