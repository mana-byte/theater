"""Meta key/value rows and the persistent send-sequence counter behind handle numbering."""

from __future__ import annotations

from tests.support.builders import running_send_job


def test_no_handles_means_zero(store):
    assert store.max_send_seq() == 0


def test_max_is_numeric_not_lexicographic(store):
    """ "#9" sorts above "#10" as text; the old ORDER BY handle DESC believed it,
    reissued handle #10, and died on the unique constraint."""
    for handle in ("aaa#9", "aaa#10"):
        store.create_job(running_send_job(handle))

    assert store.max_send_seq() == 10


def test_max_spans_every_target(store):
    """Ordering by handle sorts by target id first, so the highest sequence can
    sit under a target that is not lexicographically last."""
    store.create_job(running_send_job("zzz#2"))
    store.create_job(running_send_job("aaa#7"))

    assert store.max_send_seq() == 7


def test_spawn_handles_without_a_sequence_are_ignored(store):
    store.create_job(running_send_job("plain-handle"))
    store.create_job(running_send_job("aaa#3"))

    assert store.max_send_seq() == 3


def test_get_meta_absent_key_returns_none(store):
    assert store.get_meta("nope") is None


def test_set_meta_then_get_meta_round_trips(store):
    store.set_meta("colour", "blue")
    assert store.get_meta("colour") == "blue"


def test_set_meta_twice_updates_not_raises(store):
    store.set_meta("n", "1")
    store.set_meta("n", "2")
    assert store.get_meta("n") == "2"


def test_get_send_seq_returns_zero_on_fresh_db(store):
    assert store.get_send_seq() == 0


def test_set_send_seq_round_trips(store):
    store.set_send_seq(42)
    assert store.get_send_seq() == 42


def test_send_seq_survives_job_deletion(store):
    """GC deleting high-numbered job rows must not regress the counter and re-mint old handles."""

    from theater.daemon.server import Daemon

    for i in range(1, 13):
        store.create_job(running_send_job(f"x#{i}"))

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
