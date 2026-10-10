"""kqueue notifications only mark a source dirty; polling is the fallback for everything else."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import select
from pathlib import Path

import pytest
from shipped import ClaudeCodeObserver

from theater.harness.channels import filesystem
from theater.harness.channels.filesystem import FilesystemWatcher, WatchGate
from theater.harness.channels.wakeup import WakeupSignal
from theater.harness.source import TranscriptSource

pytestmark = pytest.mark.skipif(not filesystem.kqueue_available(), reason="needs kqueue")


def record(text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-06-24T18:26:15.348Z",
            "message": {
                "id": f"m-{text}",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


@pytest.fixture(autouse=True)
def _fresh_shared_watcher():
    yield
    for watcher in list(filesystem._WATCHERS.values()):
        watcher.close()


@pytest.fixture
def watched(tmp_path):
    root = tmp_path / "projects"
    (root / "-work").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    path = root / "-work" / "aaa.jsonl"
    path.write_text(json.dumps({"type": "system", "cwd": str(work)}) + "\n" + record("old") + "\n")
    s = TranscriptSource(ClaudeCodeObserver(root=root), cwd=str(work), allow_refresh=True)
    return s, path


async def attach(s: TranscriptSource) -> None:
    assert (await s.read()).attached is not None
    s.commit_attachment()


def count_stats(monkeypatch, path: Path) -> list[int]:
    calls = [0]
    real = Path.stat

    def counting(self, *args, **kwargs):
        if self == path:
            calls[0] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting)
    return calls


async def test_append_wakes_the_source_without_waiting_for_a_poll(watched):
    s, path = watched
    signal = WakeupSignal()
    s._bind_wakeup(signal.wake)
    await attach(s)
    await s.read()  # arms the watch
    signal.consume()

    with path.open("a") as fh:
        fh.write(record("new") + "\n")

    await asyncio.wait_for(signal.wait(), timeout=2)
    assert [e.text for e in (await s.read()).events] == ["new"]


async def test_quiet_watched_file_skips_the_per_poll_stat(watched, monkeypatch):
    s, path = watched
    await attach(s)
    await s.read()
    await s.read()  # caught up and watched
    calls = count_stats(monkeypatch, path)

    for _ in range(5):
        assert (await s.read()).events == ()
    assert calls[0] == 0

    with path.open("a") as fh:
        fh.write(record("new") + "\n")
    assert [e.text for e in (await s.read()).events] == ["new"]
    assert calls[0] >= 1


async def test_rotation_is_caught_by_the_directory_watch(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("a\n")
    watcher = FilesystemWatcher(asyncio.get_running_loop())
    sub = watcher.watch(path)
    assert sub is not None and sub.due()
    assert not sub.due()

    path.rename(tmp_path / "t.old")
    path.write_text("b\n")
    assert sub.due(), "rename + recreate dirties the subscription"
    assert not sub.due()

    with path.open("a") as fh:
        fh.write("c\n")
    assert sub.due(), "the file watch follows the new inode"
    watcher.close()


async def test_a_file_created_after_the_watch_is_followed(tmp_path):
    path = tmp_path / "late.jsonl"
    watcher = FilesystemWatcher(asyncio.get_running_loop())
    sub = watcher.watch(path)
    assert sub is not None and sub.due()
    path.write_text("a\n")
    assert sub.due()
    assert not sub.due()
    with path.open("a") as fh:
        fh.write("b\n")
    assert sub.due()
    watcher.close()


async def test_watcher_failure_reverts_the_source_to_full_rate_polling(watched, monkeypatch):
    s, path = watched
    await attach(s)
    await s.read()
    await s.read()
    watcher = filesystem._WATCHERS.get(asyncio.get_running_loop())
    assert watcher is not None and s._watch_gate.active

    class Broken:
        def control(self, *args, **kwargs):
            raise OSError(errno.EBADF, "simulated kqueue failure")

        def fileno(self):
            return -1

        def close(self):
            pass

    real_kq = watcher._kq
    watcher._kq = Broken()  # type: ignore[assignment]
    calls = count_stats(monkeypatch, path)

    for _ in range(4):
        await s.read()
    real_kq.close()
    assert not s._watch_gate.active
    assert calls[0] == 4, "every poll checks the file again"

    with path.open("a") as fh:
        fh.write(record("after") + "\n")
    assert [e.text for e in (await s.read()).events] == ["after"]


async def test_partial_record_never_surfaces_torn(watched):
    s, path = watched
    await attach(s)
    await s.read()
    whole = record("whole")

    with path.open("a") as fh:
        fh.write(whole[:20])
    assert (await s.read()).events == ()
    assert (await s.read()).events == ()
    with path.open("a") as fh:
        fh.write(whole[20:] + "\n")
    assert [e.text for e in (await s.read()).events] == ["whole"]


async def test_shutdown_closes_the_kqueue_and_removes_the_reader(watched):
    s, _ = watched
    await attach(s)
    await s.read()
    watcher = filesystem._WATCHERS.get(asyncio.get_running_loop())
    assert watcher is not None and watcher._kq is not None
    kq_fd = watcher._kq.fileno()
    entry_fds = [e.fd for e in watcher._entries.values() if e.fd >= 0]

    await s.aclose()

    assert watcher.closed
    for fd in (kq_fd, *entry_fds):
        with pytest.raises(OSError) as info:
            os.fstat(fd)
        assert info.value.errno == errno.EBADF
    assert asyncio.get_running_loop().remove_reader(kq_fd) is False


async def test_unwatchable_sources_keep_polling(watched, monkeypatch):
    monkeypatch.setattr(filesystem, "shared_watcher", lambda: None)
    s, path = watched
    await attach(s)
    await s.read()
    calls = count_stats(monkeypatch, path)

    await s.read()
    await s.read()
    assert calls[0] == 2
    with path.open("a") as fh:
        fh.write(record("new") + "\n")
    assert [e.text for e in (await s.read()).events] == ["new"]


async def test_descriptor_cap_means_polling(tmp_path, monkeypatch):
    monkeypatch.setattr(filesystem, "MAX_WATCH_FDS", 2)
    watcher = FilesystemWatcher(asyncio.get_running_loop())
    first = tmp_path / "a"
    first.write_text("x")
    assert watcher.watch(first) is not None  # dir + file
    other = tmp_path / "sub"
    other.mkdir()
    (other / "b").write_text("x")
    assert watcher.watch(other / "b") is None
    watcher.close()


async def test_gate_without_watcher_always_asks_for_a_real_check(tmp_path, monkeypatch):
    monkeypatch.setattr(select, "kqueue", None, raising=False)
    monkeypatch.setattr(filesystem, "kqueue_available", lambda: False)
    gate = WatchGate()
    path = tmp_path / "x"
    assert gate.due(path) and gate.due(path)
    assert not gate.active


async def test_pending_receipt_is_stat_polled_only_when_its_directory_changes(
    tmp_path, monkeypatch
):
    from theater.harness.builtin.plugins.claude.source import _open_claude_source

    root = tmp_path / "projects"
    (root / "-work").mkdir(parents=True)
    expected = root / "-work" / "sid.jsonl"
    s = _open_claude_source(root=root, relocate_by_cwd=False, cwd=str(tmp_path))
    s.admit_exact_location(location=str(expected), session_id="sid")
    assert (await s.read()).waiting  # first check arms the watch
    calls = count_stats(monkeypatch, expected)

    for _ in range(4):
        assert (await s.read()).waiting
    assert calls[0] == 0

    expected.write_text(json.dumps({"type": "system", "cwd": str(tmp_path)}) + "\n")
    batch = await s.read()
    assert calls[0] >= 1 and batch.attached is not None
    await s.aclose()


def _run(loop, coro):
    return loop.run_until_complete(coro)


def _fds(watcher: FilesystemWatcher) -> list[int]:
    assert watcher._kq is not None
    return [watcher._kq.fileno(), *(e.fd for e in watcher._entries.values() if e.fd >= 0)]


def test_one_watcher_per_loop_and_migration_closes_the_old_one(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("a\n")
    loop_a, loop_b = asyncio.new_event_loop(), asyncio.new_event_loop()
    migrant, peer_a, peer_b = WatchGate(), WatchGate(), WatchGate()

    async def arm(gate):
        assert gate.due(path)
        return filesystem.shared_watcher()

    try:
        watcher_a = _run(loop_a, arm(migrant))
        assert _run(loop_a, arm(peer_a)) is watcher_a, "one watcher per loop"
        watcher_b = _run(loop_b, arm(peer_b))
        assert watcher_b is not watcher_a
        old_fds, b_fds = _fds(watcher_a), _fds(watcher_b)

        _run(loop_b, arm(migrant))  # A -> B; B's existing watch is shared
        assert _fds(watcher_b) == b_fds, "dedup per loop: no new descriptors"
        assert not watcher_a.closed, "peer_a still holds A"
        peer_a.release()
        assert watcher_a.closed
        for fd in old_fds:
            with pytest.raises(OSError) as info:
                os.fstat(fd)
            assert info.value.errno == errno.EBADF
        assert {loop_b: watcher_b} == filesystem._WATCHERS

        assert _run(loop_a, arm(migrant)) is not watcher_b  # B -> A re-arms on a fresh watcher
        assert set(filesystem._WATCHERS) == {loop_a, loop_b}
        assert not watcher_b.closed, "peer_b still holds B"
    finally:
        for gate in (migrant, peer_a, peer_b):
            gate.release()
        for loop in (loop_a, loop_b):
            loop.close()


def test_closed_loops_leave_no_global_leak(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("a\n")
    dead: list[FilesystemWatcher] = []
    for _ in range(3):
        loop = asyncio.new_event_loop()

        async def arm():
            assert WatchGate().due(path)
            return filesystem.shared_watcher()

        watcher = _run(loop, arm())
        assert watcher is not None
        dead.append(watcher)
        loop.close()  # the gate is never released: the loop just dies

    loop = asyncio.new_event_loop()

    async def last():
        return filesystem.shared_watcher()

    try:
        current = _run(loop, last())
        assert all(w.closed for w in dead)
        assert {loop: current} == filesystem._WATCHERS
    finally:
        current.close()
        loop.close()
