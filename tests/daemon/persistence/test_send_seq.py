"""Meta key/value rows and the persistent send-sequence counter behind handle numbering."""

from __future__ import annotations


def test_get_send_seq_returns_zero_on_fresh_db(store):
    assert store.get_send_seq() == 0


def test_set_send_seq_round_trips(store):
    store.set_send_seq(42)
    assert store.get_send_seq() == 42
