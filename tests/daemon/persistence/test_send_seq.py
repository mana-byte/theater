"""Meta key/value rows and the persistent send-sequence counter behind handle numbering."""

from __future__ import annotations


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
