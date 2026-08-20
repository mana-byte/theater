"""Tailing a transcript: attaching, losing the file, and rotating onto a new one.

Driven through the real Claude adapter over a temporary transcript root rather
than a stub, because half of what is under test is the interplay between the
source's cursor and the adapter's `find_transcript` — a stub would only assert
that the source calls what the test told it to call.

The relocation behaviour here is deliberate and load-bearing for vibe, which
opens a fresh session directory every turn. It is also the sharpest edge in the
system: `refresh` searches by cwd *only*, so a source can rebind onto a file
that belongs to a different session in the same directory. The tests below pin
what it actually does, not what would be safe.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
from shipped import ClaudeCodeObserver

from theater.harness.source import Batch, History, TranscriptSource
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
)


def record(text: str, *, end: bool = True) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-06-24T18:26:15.348Z",
            "message": {
                "id": f"m-{text}",
                "stop_reason": "end_turn" if end else "tool_use",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


@pytest.fixture
def root(tmp_path) -> Path:
    d = tmp_path / "projects"
    (d / "-work").mkdir(parents=True)
    return d


@pytest.fixture
def workdir(tmp_path) -> str:
    d = tmp_path / "work"
    d.mkdir()
    return str(d)


def transcript(root: Path, name: str, cwd: str, *lines: str) -> Path:
    path = root / "-work" / f"{name}.jsonl"
    head = json.dumps({"type": "system", "cwd": cwd})
    path.write_text("\n".join([head, *lines]) + "\n", encoding="utf-8")
    return path


def source(root: Path, workdir: str, **kw):
    # These generic source tests exercise the relocation protocol explicitly.
    # Shipped observers opt into cwd relocation only where their format needs
    # it (Vibe); using Claude's locator here keeps the fixture compact.
    return TranscriptSource(ClaudeCodeObserver(root=root), cwd=workdir, allow_refresh=True, **kw)


async def attach(s):
    """Stage and accept the initial attachment, as the observer does."""
    batch = await s.read()
    assert batch.attached is not None
    s.commit_attachment()
    return batch


# ---- the default source does nothing, on purpose --------------------------


# ---- attaching ------------------------------------------------------------


async def test_waiting_until_the_agent_writes_its_first_record(root, workdir):
    """A spawned agent takes seconds to create its transcript; that is not death."""
    batch = await source(root, workdir).read()
    assert batch.waiting is True
    assert batch.attached is None


async def test_attaching_reports_where_it_landed(root, workdir):
    transcript(root, "aaa", workdir, record("hello"))
    s = source(root, workdir)
    batch = await s.read()
    assert batch.attached is not None
    assert batch.attached.location == str(root / "-work" / "aaa.jsonl")
    assert s.path is None, "a candidate must not mutate live state before acceptance"
    s.commit_attachment()
    assert s.path is not None


async def test_attaching_lands_at_the_end_so_history_is_not_replayed(root, workdir):
    """Re-emitting what the agent said before we arrived would fake a live turn."""
    transcript(root, "aaa", workdir, record("old"))
    s = source(root, workdir)
    await attach(s)
    assert (await s.read()).events == ()


async def test_records_written_after_the_attach_are_read(root, workdir):
    path = transcript(root, "aaa", workdir, record("old"))
    s = source(root, workdir)
    await attach(s)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record("new") + "\n")
    assert [e.text for e in (await s.read()).events] == ["new"]


# ---- losing the file ------------------------------------------------------


async def test_a_deleted_transcript_sends_the_source_back_to_searching(root, workdir):
    """Letting the read raise would kill the watcher and freeze the participant."""
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(root, workdir)
    await attach(s)
    path.unlink()

    batch = await s.read()

    assert batch.waiting is True
    assert s.path is None
    assert (s.offset, s.index, s.mtime) == (0, 0, 0), "a stale cursor would skip records"


async def test_after_losing_the_file_a_replacement_is_picked_up(root, workdir):
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(root, workdir)
    await attach(s)
    path.unlink()
    await s.read()
    transcript(root, "bbb", workdir, record("second life"))

    batch = await s.read()

    assert batch.attached is not None
    assert batch.attached.location.endswith("bbb.jsonl")


async def test_eio_on_a_trusted_pin_is_an_ordinary_source_error(root, workdir, monkeypatch):
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(
        root,
        workdir,
        session_id="aaa",
        session_provenance="operator",
        known_location=str(path),
    )
    await attach(s)
    real_stat = Path.stat

    def eio(self, *args, **kwargs):
        if self == path:
            raise OSError(errno.EIO, "simulated I/O error")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", eio)
    batch = await s.read()

    assert batch.waiting is True
    assert batch.error_code == TRANSCRIPT_SOURCE_UNAVAILABLE_CODE
    assert s.path == path


async def test_trusted_pin_requires_consecutive_absence_before_identity_loss(root, workdir):
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(
        root,
        workdir,
        session_id="aaa",
        session_provenance="operator",
        known_location=str(path),
    )
    await attach(s)
    path.unlink()

    first = await s.read()
    assert first.waiting is True
    assert first.error_code is None
    assert s.path == path

    # A replacement appearing between polls clears the pending absence rather
    # than turning a normal atomic-replace window into quarantine.
    transcript(root, "aaa", workdir, record("replacement"))
    assert (await s.read()).error_code is None
    path.unlink()
    assert (await s.read()).error_code is None

    confirmed = await s.read()
    assert confirmed.error_code == TRANSCRIPT_IDENTITY_LOST_CODE
    assert s.path == path


async def test_missing_transcript_root_is_source_unavailable_not_identity_loss(root, workdir):
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(
        root,
        workdir,
        session_id="aaa",
        session_provenance="operator",
        known_location=str(path),
        collision_domain=str(root),
    )
    await attach(s)
    path.unlink()
    path.parent.rmdir()
    root.rmdir()

    batch = await s.read()

    assert batch.error_code == TRANSCRIPT_SOURCE_UNAVAILABLE_CODE
    assert s.path == path


# ---- rotation -------------------------------------------------------------


async def test_refresh_on_a_source_with_nothing_to_find_changes_nothing(root, workdir):
    s = source(root, workdir)
    assert await s.refresh() == Batch()
    assert s.path is None


async def test_refresh_onto_the_same_file_is_an_empty_batch(root, workdir):
    """The observer reads that emptiness as 'still idle' and keeps its clocks."""
    transcript(root, "aaa", workdir, record("hello"))
    s = source(root, workdir)
    await attach(s)
    before = s.offset

    assert await s.refresh() == Batch()
    assert s.offset == before, "a re-attach here would reset the cursor for nothing"


async def test_refresh_moves_onto_a_newer_transcript(root, workdir):
    """vibe opens a new session directory every turn; staying put means going deaf."""
    old = transcript(root, "aaa", workdir, record("first turn"))
    s = source(root, workdir)
    await attach(s)

    new = transcript(root, "bbb", workdir, record("second turn"))
    import os

    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    batch = await s.refresh()

    assert batch.attached is not None
    assert s.path == old, "rotation is only a candidate until the observer accepts it"
    s.commit_attachment()
    assert s.path == new


async def test_refresh_ignores_the_session_id_it_was_opened_with(root, workdir):
    """A cwd-only refresh is staged so the observer can refuse a sibling's file.

    `refresh` searches by cwd alone — the stored id pins `find_transcript` to a
    file that never grows again, which is the bug this behaviour exists to fix.
    The source itself cannot know ownership, so it reports the candidate but
    keeps reading its accepted file until the observer decides.
    """
    mine = transcript(root, "mine", workdir, record("mine"))
    s = source(root, workdir, session_id="mine")
    await attach(s)
    assert s.path == mine

    theirs = transcript(root, "theirs", workdir, record("theirs"))
    import os

    os.utime(mine, (1000, 1000))
    os.utime(theirs, (2000, 2000))

    batch = await s.refresh()

    assert batch.attached is not None
    assert batch.attached.location == str(theirs)
    assert s.path == mine
    s.discard_attachment()
    assert s.path == mine


# ---- history --------------------------------------------------------------


async def test_history_works_on_a_source_that_never_polled(root, workdir):
    """`read_transcript` opens its own source rather than borrowing the watcher's."""
    transcript(root, "aaa", workdir, record("one"), record("two"))
    got = await source(root, workdir).history(last_n=5)
    assert [e.text for e in got.events] == ["one", "two"]
    assert got.location.endswith("aaa.jsonl")


async def test_history_returns_the_newest_events_when_asked_for_a_few(root, workdir):
    transcript(root, "aaa", workdir, record("one"), record("two"), record("three"))
    got = await source(root, workdir).history(last_n=2)
    assert [e.text for e in got.events] == ["two", "three"]


async def test_history_with_no_limit_returns_everything(root, workdir):
    transcript(root, "aaa", workdir, record("one"), record("two"))
    got = await source(root, workdir).history(last_n=0)
    assert len(got.events) == 2


async def test_history_prefers_a_persisted_pin_over_a_newer_cwd_match(root, workdir):
    mine = transcript(root, "aaa", workdir, record("mine"))
    transcript(root, "zzz", workdir, record("sibling"))

    got = await source(root, workdir, known_location=str(mine)).history(last_n=0)

    assert got.location == str(mine)
    assert [event.text for event in got.events] == ["mine"]
    assert got.correlation == "heuristic"
    assert got.pinned is True


async def test_a_missing_persisted_pin_never_falls_back_to_a_sibling(root, workdir):
    transcript(root, "zzz", workdir, record("sibling"))
    missing = root / "-work" / "gone.jsonl"

    got = await source(root, workdir, known_location=str(missing)).history(last_n=0)

    assert got.location is None
    assert got.events == ()
    assert got.pinned is True


async def test_history_leaves_the_text_unclipped(root, workdir):
    """The whole point of `read_transcript`: the job result is clipped, this is not."""
    long = "x" * 5000
    transcript(root, "aaa", workdir, record(long))
    got = await source(root, workdir).history(last_n=1)
    assert got.events[0].text == long


async def test_history_does_not_move_the_poll_cursor(root, workdir):
    path = transcript(root, "aaa", workdir, record("one"))
    s = source(root, workdir)
    await attach(s)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record("two") + "\n")

    await s.history(last_n=10)

    assert [e.text for e in (await s.read()).events] == ["two"]


async def test_usage_only_records_do_not_consume_history_or_attachment(root, workdir):
    usage = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "m-usage",
                "stop_reason": "tool_use",
                "content": [{"type": "thinking", "thinking": "hidden"}],
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        }
    )
    transcript(root, "aaa", workdir, record("visible"), usage)
    s = source(root, workdir)

    history = await s.history(last_n=1)
    attached = await s.read()

    assert [event.text for event in history.events] == ["visible"]
    assert attached.attached is not None
    assert attached.attached.last_event is None


async def test_history_of_a_transcript_that_does_not_exist_is_empty(root, workdir):
    got = await source(root, workdir).history(last_n=5)
    assert got == History()


async def test_history_without_a_cwd_has_nowhere_to_look(root):
    got = await ClaudeCodeObserver(root=root).open_source(cwd=None).history(last_n=5)
    assert got == History()


async def test_a_transcript_that_vanishes_mid_read_yields_no_events(root, workdir, monkeypatch):
    """The pane can die between locating the file and opening it."""
    transcript(root, "aaa", workdir, record("one"))
    s = source(root, workdir)
    original = Path.open

    def deny(self, *a, **kw):
        if self.suffix == ".jsonl" and "a" not in str(a[:1]):
            raise OSError("gone")
        return original(self, *a, **kw)

    got = await s.history(last_n=5)
    assert got.events

    monkeypatch.setattr(Path, "open", deny)
    assert (await s.history(last_n=5)).events == ()


# ---- StreamPoint / Attachment.point ---------------------------------------


async def test_attachment_carries_stream_point_with_dev_ino(root, workdir):
    """The attachment carries a StreamPoint with records, size, dev, and ino."""
    transcript(root, "aaa", workdir, record("one"), record("two"))
    s = source(root, workdir)
    batch = await s.read()
    assert batch.attached is not None
    assert batch.attached.point is not None
    assert batch.attached.point.records == 3  # head + 2 records
    assert batch.attached.point.size > 0
    assert batch.attached.point.dev is not None
    assert batch.attached.point.ino is not None


async def test_stream_point_dev_ino_match_fstat(root, workdir):
    """The dev/ino on the StreamPoint match the file's stat."""
    path = transcript(root, "aaa", workdir, record("one"))
    s = source(root, workdir)
    batch = await s.read()
    assert batch.attached is not None
    st = path.stat()
    assert batch.attached.point.dev == st.st_dev
    assert batch.attached.point.ino == st.st_ino


async def test_stream_point_records_grow_with_appends(root, workdir):
    """More records means a higher record count on the point."""
    path = transcript(root, "aaa", workdir, record("one"))
    s = source(root, workdir)
    batch1 = await s.read()
    s.commit_attachment()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record("two") + "\n")
    # Re-attach to get a fresh point
    s2 = source(root, workdir)
    batch2 = await s2.read()
    assert batch1.attached.point.records < batch2.attached.point.records


async def test_stream_point_is_none_for_non_file_source():
    """A non-file source produces no StreamPoint — backward compatible."""
    from theater.harness.observation import HarnessObserver
    from theater.harness.source import Batch, Source

    class _NoFileObserver(HarnessObserver):
        has_transcript = False

        def is_idle_screen(self, capture):
            return False

    class _NoFileSource(Source):
        async def read(self):
            return Batch()

    s = _NoFileSource()
    batch = await s.read()
    # No attachment at all — point is None by absence, not by construction.
    assert batch.attached is None


# ---- stream_floor hook -----------------------------------------------------


def test_stream_floor_returns_none_for_nonexistent_file(root):
    """stream_floor returns None for a missing file, not a partial fact."""
    from shipped import ClaudeCodeObserver

    obs = ClaudeCodeObserver(root=root)
    assert obs.stream_floor(str(root / "-work" / "missing.jsonl")) is None


def test_stream_floor_returns_point_for_existing_file(root, workdir):
    """stream_floor returns a StreamPoint with records, size, dev, ino."""
    from shipped import ClaudeCodeObserver

    path = transcript(root, "aaa", workdir, record("one"), record("two"))
    obs = ClaudeCodeObserver(root=root)
    point = obs.stream_floor(str(path))
    assert point is not None
    assert point.records == 3  # head + 2 records
    assert point.size > 0
    assert point.dev is not None
    assert point.ino is not None
    st = path.stat()
    assert point.dev == st.st_dev
    assert point.ino == st.st_ino


def test_stream_floor_default_is_none():
    """The base HarnessObserver.stream_floor returns None by default."""
    from theater.harness.observation import HarnessObserver

    class _Bare(HarnessObserver):
        def is_idle_screen(self, capture):
            return False

    assert _Bare().stream_floor("/anywhere") is None
