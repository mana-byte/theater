"""Tailing a live transcript into the bus.

Everything here runs against a temporary Vibe-shaped tree, never the real
~/.vibe. The harness is constructed with an explicit root for exactly that
reason.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from theater.daemon.observer import (
    AWAITING_INPUT_TIMEOUT,
    RELOCATE_TIMEOUT,
    Observer,
    TranscriptCursor,
)
from theater.harness.vibe import VibeHarness
from theater.models import Status

USER = {"role": "user", "content": "do the thing"}
WORKING = {
    "role": "assistant",
    "content": "on it",
    "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}],
}
RESULT = {"role": "tool", "content": "output", "name": "bash", "tool_call_id": "c1"}
DONE = {"role": "assistant", "content": "finished"}


@pytest.fixture
def vibe_tree(tmp_path):
    """A Vibe log root with one session whose cwd is a temporary project."""
    root = tmp_path / "logs" / "session"
    session = root / "session_20260101_000000_deadbeef"
    session.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    (session / "meta.json").write_text(
        json.dumps(
            {
                "session_id": "deadbeef-1111-2222-3333",
                "environment": {"working_directory": str(project)},
            }
        )
    )
    transcript = session / "messages.jsonl"
    transcript.write_text("")
    return {"root": root, "project": project, "transcript": transcript}


def append(path: Path, *records: dict) -> None:
    with path.open("a") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
        fh.flush()


async def until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.fixture
async def observing(registry, vibe_tree):
    """An observer wound tight enough to finish inside a test."""
    observer = Observer(
        registry,
        {"vibe": VibeHarness(root=vibe_tree["root"])},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )
    observer.start()
    yield observer
    await observer.aclose()


def kinds(store, prefix="agent."):
    return [
        row["kind"]
        for row in store.bus_tail(limit=500)
        if row["kind"].startswith(prefix)
    ]


async def test_new_records_reach_the_bus_as_normalized_events(
    registry, vibe_tree, observing
):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in kinds(registry.store))

    append(vibe_tree["transcript"], USER, WORKING, RESULT, DONE)

    assert await until(lambda: "agent.assistant" in kinds(registry.store))
    assert kinds(registry.store) == [
        "agent.transcript",
        "agent.user",
        "agent.assistant",
        "agent.tool_call",
        "agent.tool_result",
        "agent.assistant",
    ]
    rows = [r for r in registry.store.bus_tail(limit=500) if r["kind"] == "agent.tool_call"]
    assert rows[0]["payload"]["tool"] == "bash"
    assert rows[0]["from_id"] == p.id
    # Vibe keeps no clock of its own; the payload says so rather than lying.
    assert rows[0]["payload"]["ts"] is None
    assert rows[0]["ts"] > 0


async def test_status_follows_the_turn_boundary(registry, vibe_tree, observing):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in kinds(registry.store))

    append(vibe_tree["transcript"], USER, WORKING)
    assert await until(lambda: registry.get(p.id).status is Status.WORKING)

    append(vibe_tree["transcript"], RESULT, DONE)
    assert await until(lambda: registry.get(p.id).status is Status.IDLE)


async def test_attaching_skips_history_instead_of_replaying_it(
    registry, vibe_tree, observing
):
    """An adopted agent can have megabytes behind it. None of it is news."""
    append(vibe_tree["transcript"], USER, WORKING, RESULT, DONE)
    registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in kinds(registry.store))
    await asyncio.sleep(0.1)
    assert kinds(registry.store) == ["agent.transcript"]

    attach = [
        r for r in registry.store.bus_tail(limit=500) if r["kind"] == "agent.transcript"
    ][0]
    assert attach["payload"]["skipped_records"] == 4

    append(vibe_tree["transcript"], USER)
    assert await until(lambda: "agent.user" in kinds(registry.store))


async def test_attaching_derives_status_from_the_last_skipped_record(
    registry, vibe_tree, observing
):
    """A spawned agent that finished before we attached must not stay STARTING.

    The bus gets no history replayed, but the status must reflect the last
    record seen at attach time. This is the bug that left every spawned
    participant stuck at "starting" in the real run: the agent completed its
    turn in the 2 seconds it took the observer to find the transcript, no new
    bytes arrived after attach, and _drain never fired.
    """
    append(vibe_tree["transcript"], USER, WORKING, RESULT, DONE)
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in kinds(registry.store))
    assert await until(lambda: registry.get(p.id).status is Status.IDLE)
    # The bus carries only the transcript attach event — no history replay.
    assert kinds(registry.store) == ["agent.transcript"]


async def test_attaching_to_a_working_agent_sets_working(
    registry, vibe_tree, observing
):
    """If the last record is a tool call (no turn_end), status is WORKING."""
    append(vibe_tree["transcript"], USER, WORKING)
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in kinds(registry.store))
    assert await until(lambda: registry.get(p.id).status is Status.WORKING)


async def test_the_harness_session_id_is_recorded_on_the_participant(
    registry, vibe_tree, observing
):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(
        lambda: registry.get(p.id).session_id == "deadbeef-1111-2222-3333"
    )


async def test_a_half_written_record_is_not_parsed_until_it_is_complete(
    registry, vibe_tree, observing
):
    registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in kinds(registry.store))

    with vibe_tree["transcript"].open("a") as fh:
        fh.write(json.dumps(USER)[:20])
        fh.flush()
    await asyncio.sleep(0.1)
    assert kinds(registry.store) == ["agent.transcript"]

    with vibe_tree["transcript"].open("a") as fh:
        fh.write(json.dumps(USER)[20:] + "\n")
        fh.flush()
    assert await until(lambda: "agent.user" in kinds(registry.store))


async def test_a_truncated_transcript_is_re_read_from_the_top(
    registry, vibe_tree, observing
):
    registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in kinds(registry.store))
    append(vibe_tree["transcript"], USER)
    assert await until(lambda: kinds(registry.store).count("agent.user") == 1)

    vibe_tree["transcript"].write_text("")
    append(vibe_tree["transcript"], USER)
    assert await until(lambda: kinds(registry.store).count("agent.user") == 2)


async def test_a_dead_participant_stops_being_watched(registry, vibe_tree, observing):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: p.id in observing._tasks)

    registry.mark_dead(p.id)
    assert await until(lambda: p.id not in observing._tasks)

    append(vibe_tree["transcript"], USER)
    await asyncio.sleep(0.1)
    assert "agent.user" not in kinds(registry.store)


async def test_participants_of_an_unknown_harness_are_left_alone(registry, vibe_tree):
    observer = Observer(registry, {}, poll=0.01, search=0.01, sync=0.01)
    observer.start()
    try:
        registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
        await asyncio.sleep(0.1)
        assert observer._tasks == {}
        assert kinds(registry.store) == []
    finally:
        await observer.aclose()


async def test_an_unobservable_participant_is_reported_once(
    registry, vibe_tree, observing, caplog
):
    """A misreported harness name must not become a silent blind spot."""
    caplog.set_level("WARNING", logger="theater.observer")
    registry.register(
        harness="claude_code", pane=None, cwd=str(vibe_tree["project"])
    )
    assert await until(lambda: "cannot observe" in caplog.text)
    await asyncio.sleep(0.1)
    assert caplog.text.count("cannot observe") == 1
    assert "known: vibe" in caplog.text


async def test_an_observer_with_no_transcript_waits_without_spinning_out(
    registry, tmp_path
):
    """A participant we cannot find a transcript for is not an error."""
    observer = Observer(
        registry,
        {"vibe": VibeHarness(root=tmp_path / "empty")},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )
    observer.start()
    try:
        p = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
        assert await until(lambda: p.id in observer._tasks)
        await asyncio.sleep(0.1)
        assert not observer._tasks[p.id].done()
        assert kinds(registry.store) == []
    finally:
        await observer.aclose()


# ---- the cursor -------------------------------------------------------


def test_a_fresh_cursor_is_looking_for_a_file():
    cursor = TranscriptCursor()
    assert cursor.path is None
    assert (cursor.offset, cursor.index, cursor.mtime) == (0, 0, 0)


def test_advance_reports_growth_only_when_the_offset_moves():
    cursor = TranscriptCursor()
    cursor.attach(Path("/t.jsonl"), offset=10, index=2, mtime=1)
    assert cursor.advance(10, 2, 5) is False  # touched, not grown
    assert cursor.mtime == 5
    assert cursor.advance(40, 3, 6) is True
    assert (cursor.offset, cursor.index) == (40, 3)


def test_detaching_forgets_the_file_and_the_clocks():
    cursor = TranscriptCursor()
    cursor.attach(Path("/t.jsonl"), offset=10, index=2, mtime=1)
    cursor.begin_quiet(100.0)
    cursor.detach()
    assert cursor.path is None
    assert cursor.offset == 0
    assert cursor.quiet_since is None and cursor.screen_quiet_since is None


def test_begin_quiet_starts_the_clocks_once_and_leaves_them_running():
    cursor = TranscriptCursor()
    cursor.begin_quiet(100.0)
    cursor.begin_quiet(140.0)  # still the same silence
    assert cursor.quiet_for(140.0) == 40.0
    assert cursor.screen_quiet_for(140.0) == 40.0


def test_the_screen_clock_survives_a_relocate_resetting_nothing():
    """The v1 bug: one shared clock meant the screen check never fired.

    The relocate fires at 5s and the screen check at 10s. If the relocate
    reset the clock the screen check reads, 10s of silence never accumulates
    and AWAITING_INPUT is unreachable. Only the screen check may reset its
    own clock.
    """
    cursor = TranscriptCursor()
    cursor.begin_quiet(0.0)

    # Two relocate windows go by. Nothing resets anything.
    assert cursor.quiet_for(6.0) > RELOCATE_TIMEOUT
    assert cursor.quiet_for(11.0) > RELOCATE_TIMEOUT

    # The screen check is still reachable, and throttles only itself.
    assert cursor.screen_quiet_for(11.0) > AWAITING_INPUT_TIMEOUT
    cursor.screen_quiet_since = 11.0
    assert cursor.screen_quiet_for(12.0) < AWAITING_INPUT_TIMEOUT
    assert cursor.quiet_for(12.0) == 12.0  # relocate clock untouched


def test_new_bytes_restart_both_clocks():
    cursor = TranscriptCursor()
    cursor.begin_quiet(0.0)
    cursor.screen_quiet_since = 5.0
    cursor.stir()
    assert cursor.quiet_since is None and cursor.screen_quiet_since is None
    assert cursor.quiet_for(20.0) == 0.0
