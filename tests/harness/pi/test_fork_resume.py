"""Pi fork and resume switches attach at the new session boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.support.pi_transcripts import (
    append,
    mark_switch,
    message_record,
    session_record,
    write_switch_marker,
)
from theater.harness.builtin.plugins.pi.constants import (
    PI_SWITCH_MARKER_VERSION,
)
from theater.harness.builtin.plugins.pi.observer import PiObserver


def _mark_startup_fork(
    root: Path, *, previous: Path, target: Path, offset: int | None = None
) -> None:
    stat = target.stat()
    write_switch_marker(
        root,
        target,
        {
            "version": PI_SWITCH_MARKER_VERSION,
            "reason": "startup-fork",
            "location": str(target.resolve()),
            "previous_location": str(previous.resolve()),
            "offset": stat.st_size if offset is None else offset,
            "dev": stat.st_dev,
            "ino": stat.st_ino,
        },
    )


def test_pi_fork_switch_skips_copied_history_and_observes_only_new_records(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    old = sessions / "old.jsonl"
    copied = message_record(
        "copied-assistant",
        {
            "role": "assistant",
            "content": "already accounted",
            "stopReason": "stop",
            "usage": {"input": 100, "output": 1},
        },
    )
    append(old, session_record(session_id="old", cwd=workdir), copied)
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="old", after=1_800_000_000
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()
    assert [
        event.usage.input_tokens for event in asyncio.run(source.read()).events if event.usage
    ] == [100]
    source.acknowledge_source_checkpoint()

    fork = sessions / "fork.jsonl"
    mark_switch(sessions, previous=old, target=fork, reason="fork", records=2)
    append(
        fork,
        session_record(session_id="fork", cwd=workdir),
        copied,
        message_record(
            "new-assistant",
            {
                "role": "assistant",
                "content": "new work",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )

    attached = asyncio.run(source.refresh()).attached
    assert attached is not None
    assert attached.session_id == "fork"
    assert attached.skipped == 2
    source.commit_attachment()
    batch = asyncio.run(source.read())
    assert [event.text for event in batch.events if event.text] == ["new work"]
    assert [event.usage.input_tokens for event in batch.events if event.usage] == [7]


def test_pi_startup_fork_waits_for_boundary_and_never_replays_copied_history(tmp_path) -> None:
    workdir = tmp_path / "work"
    predecessor_root = tmp_path / "predecessor"
    successor_root = tmp_path / "successor"
    workdir.mkdir()
    predecessor_root.mkdir()
    successor_root.mkdir()
    predecessor = predecessor_root / "old.jsonl"
    copied = message_record(
        "copied-assistant",
        {
            "role": "assistant",
            "content": "already accounted",
            "stopReason": "stop",
            "usage": {"input": 100, "output": 1},
        },
    )
    append(predecessor, session_record(session_id="old", cwd=workdir), copied)
    fork = successor_root / "fork.jsonl"
    append(
        fork,
        session_record(session_id="successor", cwd=workdir, parent_session=predecessor),
        copied,
    )
    copied_offset = fork.stat().st_size
    source = PiObserver(root=successor_root, isolated=True).open_source(
        cwd=str(workdir), session_id="successor"
    )

    # Pi creates the file before extension session_start writes the boundary.
    assert asyncio.run(source.read()).waiting is True

    unrelated = predecessor_root / "unrelated.jsonl"
    append(unrelated, session_record(session_id="unrelated", cwd=workdir))
    _mark_startup_fork(
        successor_root,
        previous=unrelated,
        target=fork,
        offset=copied_offset,
    )
    assert asyncio.run(source.read()).waiting is True

    _mark_startup_fork(
        successor_root,
        previous=predecessor,
        target=fork,
        offset=copied_offset,
    )
    append(
        fork,
        message_record(
            "new-assistant",
            {
                "role": "assistant",
                "content": "new work",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.session_id == "successor"
    assert attached.skipped == 2
    source.commit_attachment()

    batch = asyncio.run(source.read())
    assert [event.text for event in batch.events if event.text] == ["new work"]
    assert [event.usage.input_tokens for event in batch.events if event.usage] == [7]


def test_pi_startup_fork_restart_reconciles_only_successor_usage(tmp_path) -> None:
    workdir = tmp_path / "work"
    predecessor_root = tmp_path / "predecessor"
    successor_root = tmp_path / "successor"
    workdir.mkdir()
    predecessor_root.mkdir()
    successor_root.mkdir()
    predecessor = predecessor_root / "old.jsonl"
    copied = message_record(
        "copied-assistant",
        {
            "role": "assistant",
            "content": "already accounted",
            "stopReason": "stop",
            "usage": {"input": 100, "output": 1},
        },
    )
    append(predecessor, session_record(session_id="old", cwd=workdir), copied)
    fork = successor_root / "fork.jsonl"
    append(
        fork,
        session_record(session_id="successor", cwd=workdir, parent_session=predecessor),
        copied,
    )
    _mark_startup_fork(successor_root, previous=predecessor, target=fork)
    append(
        fork,
        message_record(
            "new-assistant",
            {
                "role": "assistant",
                "content": "new work",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    restarted = PiObserver(root=successor_root, isolated=True).open_source(
        cwd=str(workdir),
        session_id="successor",
        known_location=str(fork),
    )

    assert asyncio.run(restarted.read()).attached is not None
    restarted.commit_attachment()
    batch = asyncio.run(restarted.read())
    assert [event.usage.input_tokens for event in batch.events if event.usage] == [7]
    assert all(event.usage_only for event in batch.events)
    assert batch.trajectory == ()


def test_pi_restart_uses_archived_fork_boundary_after_a_later_new(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    previous = tmp_path / "previous.jsonl"
    forked = sessions / "forked.jsonl"
    append(
        previous,
        session_record(session_id="previous", cwd=workdir),
        message_record(
            "old",
            {
                "role": "assistant",
                "content": "copied",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    append(
        forked,
        session_record(session_id="forked", cwd=workdir, parent_session=previous),
        message_record(
            "old",
            {
                "role": "assistant",
                "content": "copied",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    _mark_startup_fork(sessions, previous=previous, target=forked)
    append(
        forked,
        message_record(
            "new",
            {
                "role": "assistant",
                "content": "new",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    replacement = sessions / "replacement.jsonl"
    append(replacement, session_record(session_id="replacement", cwd=workdir))
    mark_switch(sessions, previous=forked, target=replacement, reason="new")

    restarted = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="forked", known_location=str(forked)
    )
    assert asyncio.run(restarted.read()).attached is not None
    restarted.commit_attachment()
    batch = asyncio.run(restarted.read())
    assert [event.usage.input_tokens for event in batch.events if event.usage] == [7]
    assert all(event.usage_only for event in batch.events)
    assert batch.trajectory == ()


def test_pi_existing_resume_switch_attaches_at_switch_eof(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    current = sessions / "current.jsonl"
    target = sessions / "existing.jsonl"
    append(current, session_record(session_id="current", cwd=workdir))
    append(
        target,
        session_record(session_id="existing", cwd=workdir, timestamp="2020-01-01T00:00:00.000Z"),
        message_record(
            "historical",
            {
                "role": "assistant",
                "content": "historical",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="current", after=1_800_000_000
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()
    asyncio.run(source.read())
    source.acknowledge_source_checkpoint()

    mark_switch(sessions, previous=current, target=target, reason="resume", records=2)
    append(
        target,
        message_record(
            "new-on-resume",
            {
                "role": "assistant",
                "content": "after resume",
                "stopReason": "stop",
                "usage": {"input": 13, "output": 2},
            },
        ),
    )
    attached = asyncio.run(source.refresh()).attached
    assert attached is not None
    assert attached.session_id == "existing"
    assert attached.skipped == 2
    source.commit_attachment()
    batch = asyncio.run(source.read())
    assert [event.text for event in batch.events if event.text] == ["after resume"]
    assert [event.usage.input_tokens for event in batch.events if event.usage] == [13]
