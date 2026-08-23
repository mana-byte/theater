"""Observability gauge count tests (plan §9.2)."""

from __future__ import annotations

from theater.daemon.jobs import JobManager
from theater.models import Job, JobState, Participant, Status, Tier, now


def test_participant_counts_match_domain_semantics(store, registry):
    spawned = Participant(harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1", cwd="/tmp")
    adopted = Participant(harness="vibe", tier=Tier.ADOPTED, tmux_pane="%2", cwd="/tmp")
    external_with_pane = Participant(harness="vibe", tier=Tier.EXTERNAL, tmux_pane="%3", cwd="/tmp")
    spawned_no_pane = Participant(harness="vibe", tier=Tier.SPAWNED, cwd="/tmp")
    dead_spawned = Participant(harness="vibe", tier=Tier.SPAWNED, cwd="/tmp")
    for p in (spawned, adopted, external_with_pane, spawned_no_pane, dead_spawned):
        store.upsert_participant(p)
    store.set_status(dead_spawned.id, Status.DEAD)

    all_rows = store.list_participants(include_dead=True)
    assert store.live_count() == sum(1 for p in all_rows if p.status is not Status.DEAD)
    assert store.addressable_count() == sum(1 for p in all_rows if p.addressable)
    assert store.live_count() == 4
    assert store.addressable_count() == 3
    assert registry.live_count() == store.live_count()
    assert registry.addressable_count() == store.addressable_count()


def test_job_active_count_covers_all_states_and_forwards(store):
    for handle, state in (
        ("r1", JobState.RUNNING),
        ("r2", JobState.RUNNING),
        ("d1", JobState.DONE),
        ("c1", JobState.CRASHED),
        ("k1", JobState.KILLED),
    ):
        store.create_job(
            Job(
                handle=handle,
                caller_id="cli",
                target_id="t",
                kind="send",
                prompt=None,
                state=state,
                result=None,
                error_code=None,
                created_at=now(),
                finished_at=now() if state is not JobState.RUNNING else None,
            )
        )
    assert store.active_job_count() == 2

    jm = JobManager(store)
    assert jm.active_count() == 2
    jm.finish("r1", state=JobState.DONE, result="ok")
    assert jm.active_count() == 1
    jm.finish("r2", state=JobState.KILLED, error_code="killed")
    assert jm.active_count() == 0
    assert store.active_job_count() == 0


def test_counts_zero_on_empty_store(store):
    assert store.live_count() == 0
    assert store.addressable_count() == 0
    assert store.active_job_count() == 0
