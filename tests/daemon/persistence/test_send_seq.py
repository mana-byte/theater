"""Send-handle numbering has to survive a daemon restart without colliding."""

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
