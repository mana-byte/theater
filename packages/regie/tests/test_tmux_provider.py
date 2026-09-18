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
from regie.tmux.command import available, run
from regie.tmux.identity import pane_snapshot
from regie.tmux.presentation import TmuxPresentation
from regie.tmux.terminals import (
    create_terminal,
    deliver_action,
    ensure_server,
    inspect_terminal,
    managed_inventory,
    terminate_terminal,
)

pytestmark = pytest.mark.tmux


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
    stage_window = await run("display-message", "-p", "-t", "regie-provider:", "#{window_id}")
    regie_pane = await run("display-message", "-p", "-t", "regie-provider:", "#{pane_id}")
    monkeypatch.setenv("TMUX_PANE", regie_pane)
    presentation = TmuxPresentation(expected_server_identity=server)
    await presentation.stage_terminal(target, target_window=stage_window)
    assert await presentation.terminal_exists(target)
    await presentation.unstage_terminal(target)

    snapshot = await pane_snapshot(terminal_id)
    assert snapshot is not None
    assert await terminate_terminal(snapshot)
    assert not await presentation.terminal_exists(target)
    assert shutil.which("tmux") is not None
    assert "THEATER_ID" not in os.environ


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
