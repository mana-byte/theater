from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from regie.contracts import PresentationTarget
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
) -> None:
    result_path = isolated_tmux / "input.txt"
    server = await ensure_server(cwd=str(isolated_tmux))
    program = (
        "import os,time; value=input(); "
        "open(os.environ['RESULT_PATH'],'w',encoding='utf-8').write(value); time.sleep(30)"
    )
    identity = await create_terminal(
        provider_id="provider-a",
        generation=2,
        participant_id="participant-a",
        launch_id="launch-a",
        executable=sys.executable,
        argv=[sys.executable, "-c", program],
        cwd=str(isolated_tmux),
        environment={"RESULT_PATH": str(result_path)},
        presentation={"name": "provider-test", "background": True},
        expected_server_identity=server,
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
    assert result_path.read_text() == "literal! ~input"

    target = PresentationTarget(
        provider_id="provider-a",
        provider_kind="tmux",
        terminal_id=terminal_id,
        terminal_incarnation=incarnation,
        occupant=occupant,
    )
    stage_window = await run("display-message", "-p", "-t", "regie-provider:", "#{window_id}")
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
