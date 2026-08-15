"""The meta table and persistent send-sequence counter.

The send counter is the whole reason the meta table exists: today the
counter is derived from max(jobs.handle), so deleting old job rows — which
a future GC will do — makes the counter regress and re-mint handles the
deleted jobs already used. Persisting it independently of jobs fixes that.
"""

from __future__ import annotations

from theater.models import Job


def _job(handle: str) -> Job:
    return Job(
        handle=handle,
        caller_id="caller",
        target_id="target",
        kind="send",
        prompt="hi",
        state="running",
        result=None,
        error_code=None,
        created_at=1.0,
        finished_at=None,
    )


# ---- meta key/value ----------------------------------------------------------


def test_get_meta_absent_key_returns_none(store):
    assert store.get_meta("nope") is None


def test_set_meta_then_get_meta_round_trips(store):
    store.set_meta("colour", "blue")
    assert store.get_meta("colour") == "blue"


def test_set_meta_twice_updates_not_raises(store):
    store.set_meta("n", "1")
    store.set_meta("n", "2")
    assert store.get_meta("n") == "2"


# ---- send_seq ---------------------------------------------------------------


def test_get_send_seq_returns_zero_on_fresh_db(store):
    assert store.get_send_seq() == 0


def test_set_send_seq_round_trips(store):
    store.set_send_seq(42)
    assert store.get_send_seq() == 42


def test_send_seq_parse_is_numeric_not_lexicographic(store):
    """#10 must beat #9 — a lexical MAX would get this wrong."""
    store.create_job(_job("x#9"))
    store.create_job(_job("x#10"))

    assert store.max_send_seq() == 10


# ---- the regression test ----------------------------------------------------


def test_send_seq_survives_job_deletion(store):
    """The counter must not regress when high-numbered job rows are deleted.

    This is the bug the whole task exists to prevent: a future GC deletes
    old rows from jobs, and if the counter is derived from max(jobs) it
    regresses on the next daemon restart and re-mints handles the deleted
    jobs already used, silently corrupting history.

    We prove the fix works by simulating the scenario — create jobs, let
    the daemon persist the counter, delete the high-numbered rows, and
    re-run the seeding path. The counter must stay at 12, not regress to 7.
    """
    from theater.daemon.server import Daemon

    for i in range(1, 13):
        store.create_job(_job(f"x#{i}"))

    # Simulate the daemon's normal start: _init_send_seq seeds the in-memory
    # counter from max(persisted meta, max_send_seq) and _next_send_seq
    # persists after each increment.
    daemon = Daemon.__new__(Daemon)
    daemon.store = store
    daemon._send_seq = 0
    daemon._init_send_seq()
    assert daemon._send_seq == 12

    # Simulate a send that bumps the counter and persists it.
    daemon._next_send_seq()
    assert store.get_send_seq() == 13

    # Now the future GC deletes the highest job rows.
    store.conn.exec_driver_sql("DELETE FROM jobs WHERE handle LIKE 'x#1%'")
    # x#1, x#10, x#11, x#12 are gone; x#2..x#9 survive — max_send_seq is now 9.
    assert store.max_send_seq() == 9

    # The persisted meta value (13) must win over max_send_seq (9).
    daemon._send_seq = 0
    daemon._init_send_seq()
    assert daemon._send_seq == 13
