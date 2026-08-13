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

import json
from pathlib import Path

import pytest
from shipped import ClaudeCodeObserver

from theater.harness.source import Batch, History, Source


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
    return ClaudeCodeObserver(root=root).open_source(cwd=workdir, **kw)


# ---- the default source does nothing, on purpose --------------------------


async def test_a_source_that_cannot_move_or_look_back_says_so():
    """The base class is the contract; a partial implementation must not crash."""

    class Minimal(Source):
        async def read(self) -> Batch:
            return Batch()

    s = Minimal()
    assert await s.refresh() == Batch()
    assert await s.history(last_n=5) == History()
    assert await s.aclose() is None


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
    assert s.path is not None


async def test_attaching_lands_at_the_end_so_history_is_not_replayed(root, workdir):
    """Re-emitting what the agent said before we arrived would fake a live turn."""
    transcript(root, "aaa", workdir, record("old"))
    s = source(root, workdir)
    await s.read()
    assert (await s.read()).events == ()


async def test_records_written_after_the_attach_are_read(root, workdir):
    path = transcript(root, "aaa", workdir, record("old"))
    s = source(root, workdir)
    await s.read()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record("new") + "\n")
    assert [e.text for e in (await s.read()).events] == ["new"]


# ---- losing the file ------------------------------------------------------


async def test_a_deleted_transcript_sends_the_source_back_to_searching(root, workdir):
    """Letting the read raise would kill the watcher and freeze the participant."""
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(root, workdir)
    await s.read()
    path.unlink()

    batch = await s.read()

    assert batch.waiting is True
    assert s.path is None
    assert (s.offset, s.index, s.mtime) == (0, 0, 0), "a stale cursor would skip records"


async def test_after_losing_the_file_a_replacement_is_picked_up(root, workdir):
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(root, workdir)
    await s.read()
    path.unlink()
    await s.read()
    transcript(root, "bbb", workdir, record("second life"))

    batch = await s.read()

    assert batch.attached is not None
    assert batch.attached.location.endswith("bbb.jsonl")


# ---- rotation -------------------------------------------------------------


async def test_refresh_on_a_source_with_nothing_to_find_changes_nothing(root, workdir):
    s = source(root, workdir)
    assert await s.refresh() == Batch()
    assert s.path is None


async def test_refresh_onto_the_same_file_is_an_empty_batch(root, workdir):
    """The observer reads that emptiness as 'still idle' and keeps its clocks."""
    transcript(root, "aaa", workdir, record("hello"))
    s = source(root, workdir)
    await s.read()
    before = s.offset

    assert await s.refresh() == Batch()
    assert s.offset == before, "a re-attach here would reset the cursor for nothing"


async def test_refresh_moves_onto_a_newer_transcript(root, workdir):
    """vibe opens a new session directory every turn; staying put means going deaf."""
    old = transcript(root, "aaa", workdir, record("first turn"))
    s = source(root, workdir)
    await s.read()

    new = transcript(root, "bbb", workdir, record("second turn"))
    import os

    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    batch = await s.refresh()

    assert batch.attached is not None
    assert s.path == new


async def test_refresh_ignores_the_session_id_it_was_opened_with(root, workdir):
    """Known sharp edge: two agents in one directory can steal each other's file.

    `refresh` searches by cwd alone — the stored id pins `find_transcript` to a
    file that never grows again, which is the bug this behaviour exists to fix.
    The cost is that any newer transcript in the same directory wins, whoever
    wrote it. Nothing here prevents that; the test says so out loud.
    """
    mine = transcript(root, "mine", workdir, record("mine"))
    s = source(root, workdir, session_id="mine")
    await s.read()
    assert s.path == mine

    theirs = transcript(root, "theirs", workdir, record("theirs"))
    import os

    os.utime(mine, (1000, 1000))
    os.utime(theirs, (2000, 2000))

    await s.refresh()

    assert s.path == theirs


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


async def test_history_leaves_the_text_unclipped(root, workdir):
    """The whole point of `read_transcript`: the job result is clipped, this is not."""
    long = "x" * 5000
    transcript(root, "aaa", workdir, record(long))
    got = await source(root, workdir).history(last_n=1)
    assert got.events[0].text == long


async def test_history_does_not_move_the_poll_cursor(root, workdir):
    path = transcript(root, "aaa", workdir, record("one"))
    s = source(root, workdir)
    await s.read()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record("two") + "\n")

    await s.history(last_n=10)

    assert [e.text for e in (await s.read()).events] == ["two"]


async def test_history_of_a_transcript_that_does_not_exist_is_empty(root, workdir):
    got = await source(root, workdir).history(last_n=5)
    assert got == History()


async def test_history_without_a_cwd_has_nowhere_to_look(root):
    got = await ClaudeCodeObserver(root=root).open_source(cwd=None).history(last_n=5)
    assert got == History()


async def test_a_transcript_that_vanishes_mid_read_yields_no_events(
    root, workdir, monkeypatch
):
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
