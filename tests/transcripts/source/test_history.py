"""Source history reads the newest events without moving the poll cursor."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support.shipped import ClaudeCodeObserver
from tests.support.transcript_source_rig import attach, record, source, transcript
from tests.support.transcript_source_rig import root as shared_root
from tests.support.transcript_source_rig import workdir as shared_workdir
from theater.harness.source import History

root = shared_root  # the shared fixture, under the name the tests ask for
workdir = shared_workdir  # the shared fixture, under the name the tests ask for


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
