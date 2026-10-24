"""OpenCode session discovery and attachment from the storage database."""

from __future__ import annotations

import asyncio

from tests.support.opencode_recorder import attached, text
from tests.support.opencode_recorder import rec as shared_rec
from tests.support.shipped import OpenCodeObserver
from theater.models import Status
from theater.provenance import TranscriptProvenance
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
)

rec = shared_rec


def source_for(rec, workdir, **kwargs):
    return OpenCodeObserver(db=rec.path).open_source(cwd=str(workdir), **kwargs)


def test_a_missing_database_is_ordinary_waiting(tmp_path, workdir):
    observer = OpenCodeObserver(db=tmp_path / "nope.db")
    batch = asyncio.run(observer.open_source(cwd=str(workdir)).read())
    assert batch.waiting is True


def test_a_missing_database_does_not_claim_a_trusted_pin_was_lost(tmp_path, workdir):
    source = OpenCodeObserver(db=tmp_path / "nope.db").open_source_for(
        participant_id="participant",
        cwd=str(workdir),
        session_id="ses_missing",
        session_provenance=TranscriptProvenance.OPERATOR,
        known_location="opencode://ses_missing",
    )

    batch = asyncio.run(source.read())
    history = asyncio.run(source.history(last_n=0))

    assert batch.waiting is True
    assert batch.error_code == TRANSCRIPT_SOURCE_UNAVAILABLE_CODE
    assert history.error_code == TRANSCRIPT_SOURCE_UNAVAILABLE_CODE


def test_only_a_missing_pinned_session_row_is_positive_identity_loss(rec, workdir):
    source = OpenCodeObserver(db=rec.path).open_source_for(
        participant_id="participant",
        cwd=str(workdir),
        session_id="ses_missing",
        session_provenance=TranscriptProvenance.OPERATOR,
        known_location="opencode://ses_missing",
    )

    batch = asyncio.run(source.read())
    history = asyncio.run(source.history(last_n=0))

    assert batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE
    assert history.error_code == TRANSCRIPT_IDENTITY_LOST_CODE


def test_a_directory_with_no_session_is_waiting(rec, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert asyncio.run(source_for(rec, other).read()).waiting is True


def test_attaching_skips_history_and_reports_where_it_started(rec, workdir):
    rec.user("msg_u1", "hello")
    rec.step("msg_a1", rec.tick(), [text("answer")], finish="stop", times={"completed": rec.tick()})
    rec.idle("msg_i1", "succeeded")
    src = source_for(rec, workdir)

    batch = asyncio.run(src.read())

    assert batch.attached.session_id == "ses_one"
    assert batch.attached.location == "opencode://ses_one"
    assert batch.attached.skipped == rec.seq
    assert not batch.events
    src.commit_attachment()
    # Nothing new after an attach that skipped everything.
    assert not asyncio.run(src.read()).events


def test_a_session_that_finished_before_we_looked_is_idle(rec, workdir):
    """The reason `Batch.status` exists: no further event will ever arrive."""
    rec.user("msg_u1", "hello")
    rec.step("msg_a1", rec.tick(), [text("answer")], finish="stop", times={"completed": rec.tick()})
    rec.idle("msg_i1", "succeeded")
    assert asyncio.run(source_for(rec, workdir).read()).status is Status.IDLE


def test_a_session_still_working_when_we_attach_says_so(rec, workdir):
    rec.user("msg_u1", "hello")
    rec.step("msg_a1", rec.tick(), [])

    assert asyncio.run(source_for(rec, workdir).read()).status is Status.WORKING


def test_a_session_with_no_messages_is_idle(rec, workdir):
    """Launched without a prompt. Calling it WORKING would strand it there."""
    assert asyncio.run(source_for(rec, workdir).read()).status is Status.IDLE


def test_a_sub_agent_session_is_not_mistaken_for_the_pane(rec, workdir):
    """Sub-agents share their parent's directory and are always newer."""
    rec.conn.execute(
        "INSERT INTO session_v2 (id, parent_id, directory, time_created) "
        "VALUES ('ses_child', 'ses_one', ?, 9999)",
        (str(workdir.resolve()),),
    )
    rec.conn.commit()

    assert asyncio.run(source_for(rec, workdir).read()).attached.session_id == "ses_one"


def test_a_session_older_than_the_participant_is_not_ours(rec, workdir):
    """`after` is a floor for a spawn: an earlier session is someone else's."""
    batch = asyncio.run(source_for(rec, workdir, after=500.0).read())
    assert batch.waiting is True
    # `created` is 1000ms in the fixture, so a floor below it matches.
    assert asyncio.run(source_for(rec, workdir, after=0.5).read()).attached is not None


def test_a_new_session_in_the_same_process_is_picked_up(rec, workdir):
    """A generic receipt stages a fresh exact session."""
    src = source_for(rec, workdir)
    batch = asyncio.run(src.read())
    assert batch.attached is not None
    src.commit_attachment()
    rec.conn.execute(
        "INSERT INTO session_v2 (id, parent_id, directory, time_created) "
        "VALUES ('ses_two', NULL, ?, 9999)",
        (str(workdir.resolve()),),
    )
    rec.conn.commit()
    assert src.admit_exact_location(location="opencode://ses_two", session_id="ses_two") == "staged"
    batch = asyncio.run(src.read())

    assert batch.attached.session_id == "ses_two"
    assert batch.attached.correlation == str(TranscriptProvenance.EXACT)
    assert src._session is None
    src.commit_attachment()
    assert src._session == "ses_two"


def test_exact_session_without_receipt_does_not_bless_a_different_candidate(rec, workdir):
    """A trusted stored OpenCode id is not global trust for same-cwd rows."""
    src = OpenCodeObserver(db=rec.path).open_source_for(
        participant_id="participant",
        cwd=str(workdir),
        session_id="ses_one",
        session_provenance=TranscriptProvenance.EXACT,
    )
    initial = asyncio.run(src.read())
    assert initial.attached is not None
    src.commit_attachment()
    assert initial.attached.correlation == str(TranscriptProvenance.EXACT)
    rec.conn.execute(
        "INSERT INTO session_v2 (id, parent_id, directory, time_created) "
        "VALUES ('ses_two', NULL, ?, 9999)",
        (str(workdir.resolve()),),
    )
    rec.conn.commit()

    conn = src._open()
    assert conn is not None
    candidate = src._attach(conn, "ses_two")

    assert candidate.attached.session_id == "ses_two"
    assert candidate.attached.correlation == str(TranscriptProvenance.HEURISTIC)


def test_refreshing_onto_the_same_session_reports_nothing(rec, workdir):
    """An empty batch, so the observer's quiet timers keep counting."""
    src = attached(rec, workdir)
    batch = asyncio.run(src.refresh())
    assert batch.attached is None
    assert batch.progressed is False
