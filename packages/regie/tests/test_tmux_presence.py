from __future__ import annotations

import pytest
from regie.tmux.command import TmuxError
from regie.tmux.identity import PaneSnapshot
from regie.tmux.presence import observe_presence


def _pane() -> PaneSnapshot:
    return PaneSnapshot(
        server_identity="server-a",
        pane_id="%7",
        pane_pid=42,
        dead=False,
        executable="agent",
        window_id="@1",
        provider_id="provider-a",
        terminal_incarnation="incarnation-a",
        occupant_id="participant-a",
        occupant_digest="digest",
        occupant_pane_pid=42,
        launch_id="launch-a",
        launch_executable="agent",
    )


async def test_focus_and_copy_mode_evidence_is_fail_closed(monkeypatch) -> None:
    pane = _pane()
    observations = [(pane,), (pane,), (pane,), (pane,), (pane,), (pane,)]
    outputs = iter(
        (
            "focused\t0\t0\t@1\t%7\tfocus",
            "on",
            "1",
            "\t0\t0\t@1\t%7\tfocus",
            "on",
            "0",
            "focused,active-pane\t0\t0\t@1\t%8\tfocus",
            "on",
            "0",
        )
    )

    async def inventory():
        return observations.pop(0)

    async def run(*_args, **_kwargs):
        return next(outputs)

    monkeypatch.setattr("regie.tmux.presence.pane_inventory", inventory)
    monkeypatch.setattr("regie.tmux.presence.run", run)

    present = await observe_presence(pane)
    assert (present.state, present.mode) == ("present", "copy")
    blurred = await observe_presence(pane)
    assert (blurred.state, blurred.reason) == ("unknown", "focus_unverified")
    independent = await observe_presence(pane)
    assert (independent.state, independent.reason) == ("unknown", "independent_active_pane")

    async def failed(*_args, **_kwargs):
        raise TmuxError("focus inventory unavailable")

    observations.append((pane,))
    monkeypatch.setattr("regie.tmux.presence.run", failed)
    with pytest.raises(TmuxError, match="focus inventory unavailable"):
        await observe_presence(pane)
