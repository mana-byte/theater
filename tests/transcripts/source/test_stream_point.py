"""Attachments carry a stream point (dev, inode, size) and the floor of a file."""

from __future__ import annotations

from tests.support.transcript_source_rig import record, source, transcript
from tests.support.transcript_source_rig import root as shared_root
from tests.support.transcript_source_rig import workdir as shared_workdir

root = shared_root  # the shared fixture
workdir = shared_workdir  # the shared fixture


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


def test_stream_floor_returns_none_for_nonexistent_file(root):
    """stream_floor returns None for a missing file, not a partial fact."""
    from tests.support.shipped import ClaudeCodeObserver

    obs = ClaudeCodeObserver(root=root)
    assert obs.stream_floor(str(root / "-work" / "missing.jsonl")) is None


def test_stream_floor_returns_point_for_existing_file(root, workdir):
    """stream_floor returns a StreamPoint with records, size, dev, ino."""
    from tests.support.shipped import ClaudeCodeObserver

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
