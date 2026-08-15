"""Send-handle numbering has to survive a daemon restart without colliding."""

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


def test_no_handles_means_zero(store):
    assert store.max_send_seq() == 0


def test_max_is_numeric_not_lexicographic(store):
    """ "#9" sorts above "#10" as text; the old ORDER BY handle DESC believed it,
    reissued handle #10, and died on the unique constraint."""
    for handle in ("aaa#9", "aaa#10"):
        store.create_job(_job(handle))

    assert store.max_send_seq() == 10


def test_max_spans_every_target(store):
    """Ordering by handle sorts by target id first, so the highest sequence can
    sit under a target that is not lexicographically last."""
    store.create_job(_job("zzz#2"))
    store.create_job(_job("aaa#7"))

    assert store.max_send_seq() == 7


def test_spawn_handles_without_a_sequence_are_ignored(store):
    store.create_job(_job("plain-handle"))
    store.create_job(_job("aaa#3"))

    assert store.max_send_seq() == 3
