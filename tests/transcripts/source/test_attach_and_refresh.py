"""A transcript source waits, attaches, loses its file, relocates and refreshes."""

from __future__ import annotations

import errno
from pathlib import Path

from tests.support.transcript_source_rig import attach, record, source, transcript
from tests.support.transcript_source_rig import root as shared_root
from tests.support.transcript_source_rig import workdir as shared_workdir
from theater.harness.source import Batch
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
)

# Driven through the real Claude adapter over a temporary root, not a stub. `refresh` searches by
# cwd only, so a source can rebind onto another session's file; the tests pin what it does.

root = shared_root  # the shared fixture
workdir = shared_workdir  # the shared fixture


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


async def test_vanished_exact_pin_self_heals_onto_its_renamed_file(root, workdir):
    """Claude relocates its transcript's directory on a cwd change mid-session.

    Same file, same session id, new path: this must resolve on the very first
    poll after the rename, without ever reaching identity loss or requiring
    an operator `bind`.
    """
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(
        root, workdir, session_id="aaa", session_provenance="exact", known_location=str(path)
    )
    await attach(s)

    (root / "-work2").mkdir()
    new_path = root / "-work2" / "aaa.jsonl"
    path.rename(new_path)

    healed = await s.read()
    assert healed.error_code is None
    assert healed.attached is not None
    assert healed.attached.location == str(new_path)
    assert healed.attached.correlation == "exact"

    # Prove completed recovery, not just a staged candidate: commit and read
    # again to confirm the source is genuinely tailing the new file now.
    s.commit_attachment()
    assert s.path == new_path
    with new_path.open("a", encoding="utf-8") as fh:
        fh.write(record("after the move") + "\n")
    assert [e.text for e in (await s.read()).events] == ["after the move"]


async def test_vanished_exact_pin_with_no_replacement_still_quarantines(root, workdir):
    """No candidate anywhere: today's manual-recovery quarantine is unchanged."""
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(
        root, workdir, session_id="aaa", session_provenance="exact", known_location=str(path)
    )
    await attach(s)
    path.unlink()

    assert (await s.read()).error_code is None

    confirmed = await s.read()
    assert confirmed.error_code == TRANSCRIPT_IDENTITY_LOST_CODE
    assert confirmed.attached is None


async def test_vanished_exact_pin_with_duplicate_candidates_fails_closed(root, workdir):
    """Two files claiming the same session id must never be guessed between."""
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(
        root, workdir, session_id="aaa", session_provenance="exact", known_location=str(path)
    )
    await attach(s)

    (root / "-work2").mkdir()
    (root / "-work3").mkdir()
    replacement = root / "-work2" / "aaa.jsonl"
    path.rename(replacement)
    (root / "-work3" / "aaa.jsonl").write_text(replacement.read_text(), encoding="utf-8")

    assert (await s.read()).error_code is None

    confirmed = await s.read()
    assert confirmed.error_code == TRANSCRIPT_IDENTITY_LOST_CODE, (
        "an ambiguous same-session-id match must fail closed, not pick either file"
    )


async def test_vanished_exact_pin_self_heals_even_if_its_old_directory_is_gone(root, workdir):
    """A true rename can leave the old, now-empty project directory removed.

    That removal must not be mistaken for the transcript root itself going
    unavailable before the relocation lookup — which checks the harness's
    own root, not the old directory — gets a chance to run.
    """
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(
        root, workdir, session_id="aaa", session_provenance="exact", known_location=str(path)
    )
    await attach(s)

    (root / "-work2").mkdir()
    new_path = root / "-work2" / "aaa.jsonl"
    path.rename(new_path)
    path.parent.rmdir()  # the harness cleaned up its now-empty old directory

    healed = await s.read()
    assert healed.error_code is None
    assert healed.attached is not None
    assert healed.attached.location == str(new_path)


async def test_vanished_operator_pin_never_attempts_exact_relocation(root, workdir):
    """Only a source whose own session id is itself proven exact may self-heal."""
    path = transcript(root, "aaa", workdir, record("hello"))
    s = source(
        root, workdir, session_id="aaa", session_provenance="operator", known_location=str(path)
    )
    await attach(s)

    (root / "-work2").mkdir()
    path.rename(root / "-work2" / "aaa.jsonl")

    assert (await s.read()).error_code is None
    confirmed = await s.read()
    assert confirmed.error_code == TRANSCRIPT_IDENTITY_LOST_CODE, (
        "an operator-bound pin is trusted but not exact; it must still require a manual rebind"
    )


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
