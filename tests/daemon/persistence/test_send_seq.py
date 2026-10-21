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
