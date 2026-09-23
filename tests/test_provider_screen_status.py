"""Approval display uses requested provider evidence, not invented captures."""

import asyncio
from pathlib import Path

import pytest

from theater import paths
from theater.daemon.observer import QuietClock
from theater.frontend import FrontendClient
from theater.frontend.state_sync import StateSynchronizer
from theater.harness import get as get_harness
from theater.models import Status


@pytest.mark.parametrize(
    ("harness", "dialog"),
    [(name, "approval") for name in ("claude", "codex", "opencode", "pi", "vibe")]
    + [("opencode", "question"), ("vibe", "question")],
)
async def test_provider_input_request_reaches_frontend_and_clears(
    daemon, terminal_provider, harness, dialog
):
    participant = daemon.registry.register(harness=harness, pane=None, cwd=None)
    terminal = terminal_provider.bind(daemon, participant.id, command=harness)
    terminal_provider.presence[terminal] = "present"
    observer = get_harness(harness).observer
    screens = Path(__file__).parent / "fixtures" / "screens"
    client = FrontendClient(paths.socket_path(), client_id="approval-display-test")
    try:
        follow = StateSynchronizer(client)
        await follow.refresh()
        for expected in (Status.AWAITING_INPUT, Status.IDLE):
            if harness == "pi":
                state = "awaiting input" if expected is Status.AWAITING_INPUT else "idle"
                screen = f"theater: {state}"
            else:
                kind = dialog if expected is Status.AWAITING_INPUT else "idle"
                screen = (screens / f"{harness}_{kind}.txt").read_text()
            terminal_provider.screens[terminal] = screen
            await daemon.observer._screen_status_due(
                participant.id, observer, QuietClock(screen_quiet_since=0.0)
            )
            assert daemon.registry.get(participant.id).status is expected
            projected = (await follow.follow_once(wait_seconds=0)).participants[participant.id]
            assert projected.status == expected.value
            assert daemon.presence.snapshot(participant.id).state.value == "present"
        assert not terminal_provider.deliveries
        assert not terminal_provider.interruptions
        assert not terminal_provider.terminations
    finally:
        await client.close()


async def test_screen_request_is_targeted_and_waits_for_its_own_evidence(
    daemon, terminal_provider, monkeypatch
):
    participant = daemon.registry.register(harness="vibe", pane=None, cwd=None)
    terminal = terminal_provider.bind(daemon, participant.id)
    sibling = daemon.registry.register(harness="codex", pane=None, cwd=None)
    terminal_provider.bind(daemon, sibling.id)
    terminal_provider.screens[terminal] = "Esc reject"
    requests = []
    started, release = asyncio.Event(), asyncio.Event()

    async def request(provider_id, generation, method, params):
        assert method == "terminal.inspect"
        requests.append(dict(params))
        if len(requests) == 1:
            started.set()
            await release.wait()
        return await terminal_provider.request(provider_id, generation, method, params)

    monkeypatch.setattr(daemon.terminal_service.connections, "request", request)
    admission = asyncio.create_task(daemon.presence.require_absent(participant.id))
    second_admission = capture = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        second_admission = asyncio.create_task(daemon.presence.require_absent(participant.id))
        capture = asyncio.create_task(daemon.observer._capture(participant.id))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(admission, 1)
        await asyncio.wait_for(second_admission, 1)
        assert await asyncio.wait_for(capture, 1) == "Esc reject"
        assert [item["terminal_id"] for item in requests] == [terminal, terminal, terminal]
        assert "screen_max_bytes" not in requests[0]
        assert "screen_max_bytes" not in requests[1]
        assert 0 < requests[2]["screen_max_bytes"] <= 64 * 1024
        # A presence-only refresh must not lend its new timestamp to an old screen.
        await daemon.presence.require_absent(participant.id)
        assert daemon.presence.terminal_screen(participant.id) is None
    finally:
        release.set()
        for task in (admission, second_admission, capture):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
