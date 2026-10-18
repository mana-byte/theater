"""Store: durable participant, job, bus and recent-dead rows."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from theater.constants.daemon import (
    BUS_KIND_TMUX_SERVER_RESTART,
    TMUX_RESTART_TERMINATION_REASON,
    TMUX_SERVER_IDENTITY_META_KEY,
)
from theater.models import Job, JobState, Participant, Status, Tier, now


def test_roundtrip(store):
    p = Participant(
        harness="vibe",
        tier=Tier.SPAWNED,
        tmux_pane="%1",
        cwd="/tmp",
        description="Run focused participant tests",
    )
    store.upsert_participant(p)

    got = store.get_participant(p.id)
    assert got is not None
    assert got.harness == "vibe"
    assert got.tier is Tier.SPAWNED
    assert got.tmux_pane == "%1"
    assert got.description == "Run focused participant tests"


def test_tmux_restart_diagnosis_roundtrips(store):
    participant = Participant(
        harness="vibe",
        tier=Tier.SPAWNED,
        tmux_pane="%1",
        tmux_server_identity="server-before",
        termination_reason=TMUX_RESTART_TERMINATION_REASON,
        termination_incident="incident-123",
        terminated_at=123.0,
        status=Status.DEAD,
    )
    store.upsert_participant(participant)

    got = store.get_participant(participant.id)
    assert got is not None
    assert got.tmux_server_identity == "server-before"
    assert got.termination_reason == TMUX_RESTART_TERMINATION_REASON
    assert got.termination_incident == "incident-123"
    assert got.terminated_at == 123.0


def test_record_tmux_server_restart_records_diagnosis_before_advancing_identity(store):
    live = Participant(id="live", harness="vibe", tmux_pane="%1")
    dead = Participant(id="dead", harness="vibe", tmux_pane="%2", status=Status.DEAD)
    newly_owned = Participant(id="newly-owned", harness="vibe", tmux_pane="%3")
    store.upsert_participant(live)
    store.upsert_participant(dead)
    store.upsert_participant(newly_owned)
    store.set_meta(TMUX_SERVER_IDENTITY_META_KEY, "server-before")
    delivered: list[dict] = []
    store.register_bus_listener(delivered.append)

    store.record_tmux_server_restart(
        server_identity="server-after",
        affected_ids=[live.id, dead.id],
        newly_owned_ids=[newly_owned.id],
        incident="incident-123",
        terminated_at=123.0,
    )

    live_after = store.get_participant(live.id)
    dead_after = store.get_participant(dead.id)
    newly_owned_after = store.get_participant(newly_owned.id)
    assert live_after is not None
    assert dead_after is not None
    assert newly_owned_after is not None
    assert live_after.status is Status.DEAD
    assert live_after.termination_reason == TMUX_RESTART_TERMINATION_REASON
    assert live_after.termination_incident == "incident-123"
    assert live_after.terminated_at == 123.0
    assert dead_after.termination_reason is None
    assert newly_owned_after.status is not Status.DEAD
    assert newly_owned_after.tmux_server_identity == "server-after"
    assert store.get_meta(TMUX_SERVER_IDENTITY_META_KEY) == "server-after"
    incidents = [row for row in store.bus_tail() if row["kind"] == BUS_KIND_TMUX_SERVER_RESTART]
    assert len(incidents) == 1
    assert delivered == incidents


def test_record_tmux_server_restart_rolls_back_diagnosis_when_identity_write_fails(
    store, monkeypatch
):
    participant = Participant(id="live", harness="vibe", tmux_pane="%1")
    store.upsert_participant(participant)
    store.set_meta(TMUX_SERVER_IDENTITY_META_KEY, "server-before")

    def fail_identity_write(*args, **kwargs):
        raise RuntimeError("meta write failed")

    monkeypatch.setattr(store._meta, "set", fail_identity_write)
    with pytest.raises(RuntimeError, match="meta write failed"):
        store.record_tmux_server_restart(
            server_identity="server-after",
            affected_ids=[participant.id],
            newly_owned_ids=[],
            incident="incident-123",
            terminated_at=123.0,
        )

    restored = store.get_participant(participant.id)
    assert restored is not None
    assert restored.status is not Status.DEAD
    assert restored.termination_reason is None
    assert store.get_meta(TMUX_SERVER_IDENTITY_META_KEY) == "server-before"
    assert not [row for row in store.bus_tail() if row["kind"] == BUS_KIND_TMUX_SERVER_RESTART]


def test_live_resume_predecessor_claim_is_unique(store):
    predecessor = Participant(id="predecessor", harness="vibe", status=Status.DEAD)
    first = Participant(id="first", harness="vibe", resumed_from_id=predecessor.id)
    second = Participant(id="second", harness="vibe", resumed_from_id=predecessor.id)
    store.upsert_participant(predecessor)
    store.upsert_participant(first)

    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        store.upsert_participant(second)

    store.set_status(first.id, Status.DEAD)
    store.upsert_participant(second)


def test_upsert_is_idempotent(store):
    p = Participant(
        harness="vibe",
        session_id="native-session",
        session_correlation="heuristic",
        transcript_domain="/tmp/vibe-root",
        transcript_location="/tmp/vibe-root/session/messages.jsonl",
    )
    store.upsert_participant(p)
    p.harness = "claude"
    store.upsert_participant(p)

    assert len(store.list_participants()) == 1
    assert store.get_participant(p.id).harness == "claude"


def test_dead_participants_are_hidden_by_default(store):
    p = Participant(harness="vibe")
    store.upsert_participant(p)
    store.set_status(p.id, Status.DEAD)

    assert store.list_participants() == []
    assert len(store.list_participants(include_dead=True)) == 1


def test_find_by_pane_ignores_the_dead(store):
    old = Participant(harness="vibe", tmux_pane="%1", status=Status.DEAD)
    new = Participant(harness="claude", tmux_pane="%1")
    store.upsert_participant(old)
    store.upsert_participant(new)

    found = store.find_by_pane("%1")
    assert found is not None
    assert found.id == new.id


def test_bus_is_ordered_and_seekable(store):
    first = store.bus_append("a", payload={"n": 1})
    store.bus_append("b", payload={"n": 2})

    tail = store.bus_tail()
    assert [e["kind"] for e in tail] == ["a", "b"]
    assert tail[0]["payload"] == {"n": 1}

    assert [e["kind"] for e in store.bus_tail(after_id=first)] == ["b"]


def test_exact_bus_record_requires_participant_and_allowlisted_kind(store):
    row_id = store.bus_append("agent.send", from_id="p", to_id="child")
    disallowed_id = store.bus_append("participant.status", to_id="p")

    assert store.bus_record_for_participant("p", row_id, kinds={"agent.send"})["id"] == row_id
    assert store.bus_record_for_participant("other", row_id, kinds={"agent.send"}) is None
    assert store.bus_record_for_participant("p", disallowed_id, kinds={"agent.send"}) is None


def test_reopening_does_not_wipe_state(store, theater_home):
    from theater import paths
    from theater.daemon.store import Store

    p = Participant(
        harness="vibe",
        session_id="native-session",
        session_correlation="heuristic",
        transcript_domain="/tmp/vibe-root",
        transcript_location="/tmp/vibe-root/session/messages.jsonl",
        description="Survives a daemon restart",
    )
    store.upsert_participant(p)
    store.close()

    again = Store(paths.db_path())
    restored = again.get_participant(p.id)
    assert restored is not None
    assert restored.session_id == "native-session"
    assert restored.session_correlation == "heuristic"
    assert restored.transcript_domain == "/tmp/vibe-root"
    assert restored.transcript_location == "/tmp/vibe-root/session/messages.jsonl"
    assert restored.description == "Survives a daemon restart"
    again.close()


def test_a_persisted_starting_status_loads_as_idle(store):
    """A persisted starting status loads as IDLE for database compatibility."""
    from theater.daemon.store import participants

    p = Participant(harness="vibe", tier=Tier.SPAWNED)
    store.upsert_participant(p)

    # Bypass the enum to model a persisted value from an earlier database.
    store.conn.execute(
        participants.update().where(participants.c.id == p.id).values(status="starting")
    )

    got = store.get_participant(p.id)
    assert got is not None
    assert got.status is Status.IDLE


def test_name_is_never_persisted_to_sqlite(store):
    """The runtime name must not become a SQLite column.

    A participant written through the registry has a name on the object, but
    reading the same row directly from the store — bypassing the registry's
    in-memory map — yields a participant whose ``name`` is ``None``.
    """
    from theater.daemon.registry import Registry
    from theater.daemon.schema import participants as participants_table

    reg = Registry(store)
    p = reg.create_spawned(harness="vibe", cwd="/tmp")
    assert p.name is not None  # the registry assigned one

    raw = store.get_participant(p.id)
    assert raw is not None
    assert raw.name is None
    assert "name" not in participants_table.c
    assert "description" in participants_table.c


def test_job_structured_fields_round_trip(store):
    """response_format, structured_result, and structured_status persist."""
    store.create_job(
        Job(
            handle="j1",
            caller_id="cli",
            target_id="p1",
            kind="send",
            prompt="go",
            state=JobState.RUNNING,
            result=None,
            error_code=None,
            created_at=now(),
            finished_at=None,
            response_format='{"type":"json_schema"}',
            structured_result='{"answer": 42}',
            structured_status="parsed",
        )
    )
    job = store.get_job("j1")
    assert job is not None
    assert job.response_format == '{"type":"json_schema"}'
    assert job.structured_result == '{"answer": 42}'
    assert job.structured_status == "parsed"


def test_job_structured_fields_default_to_none(store):
    """A job created without structured fields has them as None."""
    store.create_job(
        Job(
            handle="j2",
            caller_id="cli",
            target_id="p1",
            kind="send",
            prompt="go",
            state=JobState.RUNNING,
            result=None,
            error_code=None,
            created_at=now(),
            finished_at=None,
        )
    )
    job = store.get_job("j2")
    assert job is not None
    assert job.response_format is None
    assert job.structured_result is None
    assert job.structured_status is None


def test_finish_job_carries_structured_fields(store):
    """finish_job writes structured fields alongside state and result."""
    store.create_job(
        Job(
            handle="j3",
            caller_id="cli",
            target_id="p1",
            kind="send",
            prompt="go",
            state=JobState.RUNNING,
            result=None,
            error_code=None,
            created_at=now(),
            finished_at=None,
        )
    )
    store.finish_job(
        "j3",
        state=JobState.DONE,
        result="ok",
        finished_at=now(),
        response_format='{"type":"json_schema"}',
        structured_result='{"answer": 99}',
        structured_status="parsed",
    )
    job = store.get_job("j3")
    assert job is not None
    assert job.response_format == '{"type":"json_schema"}'
    assert job.structured_result == '{"answer": 99}'
    assert job.structured_status == "parsed"


def test_job_to_dict_includes_structured_fields(store):
    """to_dict serialises the structured fields."""
    store.create_job(
        Job(
            handle="j4",
            caller_id="cli",
            target_id="p1",
            kind="send",
            prompt="go",
            state=JobState.RUNNING,
            result=None,
            error_code=None,
            created_at=now(),
            finished_at=None,
            response_format="schema",
            structured_result="result",
            structured_status="parsed",
        )
    )
    d = store.get_job("j4").to_dict()
    assert d["response_format"] == "schema"
    assert d["structured_result"] == "result"
    assert d["structured_status"] == "parsed"


def test_finish_job_without_structured_fields_keeps_legacy_semantics(store):
    """finish_job without structured fields leaves them null (legacy path)."""
    store.create_job(
        Job(
            handle="j5",
            caller_id="cli",
            target_id="p1",
            kind="send",
            prompt="go",
            state=JobState.RUNNING,
            result=None,
            error_code=None,
            created_at=now(),
            finished_at=None,
        )
    )
    store.finish_job("j5", state=JobState.DONE, result="ok", finished_at=now())
    job = store.get_job("j5")
    assert job is not None
    assert job.result == "ok"
    assert job.response_format is None
    assert job.structured_result is None
    assert job.structured_status is None


def _five_participants(store) -> list[Participant]:
    """Create five participants in order and return them."""
    ps = []
    for i in range(5):
        p = Participant(harness="vibe", cwd=f"/tmp/{i}")
        store.upsert_participant(p)
        ps.append(p)
    return ps


def test_list_participants_ids_omitted_returns_all(store):
    """ids=None (absent) behaves identically to today — every live row."""
    ps = _five_participants(store)
    rows = store.list_participants()
    assert [r.id for r in rows] == [p.id for p in ps]


def test_list_participants_ids_subset_preserves_creation_order(store):
    """ids=[a, c] out of 5 returns exactly 2, in creation order."""
    ps = _five_participants(store)
    ids = [ps[0].id, ps[2].id]
    rows = store.list_participants(ids=ids)
    # Creation order (ASC by created_at) must be preserved, not the order of ids.
    assert [r.id for r in rows] == [ps[0].id, ps[2].id]


def test_list_participants_ids_empty_returns_nothing(store):
    """ids=[] is the trap: it must return [] not everything."""
    _five_participants(store)
    rows = store.list_participants(ids=[])
    assert rows == []


def test_list_participants_ids_unknown_silently_omitted(store):
    """Unknown ids are dropped with no error."""
    rows = store.list_participants(ids=["ghost-id"])
    assert rows == []


def test_list_participants_ids_dead_excluded_without_include_dead(store):
    """A dead participant named in ids is omitted when include_dead=False."""
    p = Participant(harness="vibe")
    store.upsert_participant(p)
    store.set_status(p.id, Status.DEAD)
    rows = store.list_participants(ids=[p.id])
    assert rows == []


def test_list_participants_ids_dead_included_with_include_dead(store):
    """A dead participant named in ids is returned when include_dead=True."""
    p = Participant(harness="vibe")
    store.upsert_participant(p)
    store.set_status(p.id, Status.DEAD)
    rows = store.list_participants(ids=[p.id], include_dead=True)
    assert len(rows) == 1
    assert rows[0].id == p.id


def test_list_participants_keyset_orders_ties_and_excludes_the_cursor(store):
    rows = [
        Participant(id="p-b", harness="vibe", created_at=1.0),
        Participant(id="p-a", harness="vibe", created_at=1.0),
        Participant(id="p-c", harness="vibe", created_at=2.0),
    ]
    for participant in rows:
        store.upsert_participant(participant)

    first = store.list_participants(limit=2)
    second = store.list_participants(
        after=(first[-1].created_at, first[-1].id),
        limit=2,
    )
    end = store.list_participants(
        after=(second[-1].created_at, second[-1].id),
        limit=2,
    )

    assert [participant.id for participant in first] == ["p-a", "p-b"]
    assert [participant.id for participant in second] == ["p-c"]
    assert end == []


def test_list_recent_dead_returns_dead_ordered_by_last_activity(store):
    p1 = Participant(harness="vibe", cwd="/tmp/a", session_id="s1")
    store.upsert_participant(p1)
    p2 = Participant(harness="codex", cwd="/tmp/b", session_id="s2")
    store.upsert_participant(p2)
    store.set_status(p1.id, Status.DEAD)
    store.set_status(p2.id, Status.DEAD)
    rows = store.list_recent_dead(limit=20)
    assert {r.id for r in rows} == {p1.id, p2.id}


def test_list_recent_dead_excludes_alive(store):
    p = Participant(harness="vibe", cwd="/tmp/x")
    store.upsert_participant(p)
    rows = store.list_recent_dead(limit=20)
    assert rows == []


def test_list_recent_dead_respects_limit(store):
    for i in range(5):
        p = Participant(harness="vibe", cwd=f"/tmp/{i}", session_id=f"s{i}")
        store.upsert_participant(p)
        store.set_status(p.id, Status.DEAD)
    rows = store.list_recent_dead(limit=2)
    assert len(rows) == 2


def test_list_recent_dead_excludes_without_session_id(store):
    p = Participant(harness="vibe", cwd="/tmp/x")
    store.upsert_participant(p)
    store.set_status(p.id, Status.DEAD)
    assert store.list_recent_dead(limit=20) == []


def test_list_recent_dead_excludes_empty_string_session_id(store):
    p = Participant(harness="vibe", cwd="/tmp/x", session_id="")
    store.upsert_participant(p)
    store.set_status(p.id, Status.DEAD)
    assert store.list_recent_dead(limit=20) == []


def test_list_recent_dead_filter_before_limit(store):
    from theater.daemon.schema import participants
    from theater.models import now as _now

    recent = Participant(harness="vibe", cwd="/tmp/recent")
    store.upsert_participant(recent)
    store.set_status(recent.id, Status.DEAD)

    older = Participant(harness="vibe", cwd="/tmp/older", session_id="valid")
    store.upsert_participant(older)
    store.set_status(older.id, Status.DEAD)
    store.conn.execute(
        participants.update()
        .where(participants.c.id == older.id)
        .values(last_activity=_now() - 999999)
    )

    rows = store.list_recent_dead(limit=1)
    assert len(rows) == 1
    assert rows[0].id == older.id


def test_list_recent_dead_excludes_live_session_ids(store):
    from theater.daemon.schema import participants
    from theater.models import now as _now

    held = Participant(harness="vibe", cwd="/tmp/held", session_id="held")
    store.upsert_participant(held)
    store.set_status(held.id, Status.DEAD)
    store.conn.execute(
        participants.update().where(participants.c.id == held.id).values(last_activity=_now())
    )

    allowed = Participant(harness="vibe", cwd="/tmp/allowed", session_id="free")
    store.upsert_participant(allowed)
    store.set_status(allowed.id, Status.DEAD)
    store.conn.execute(
        participants.update()
        .where(participants.c.id == allowed.id)
        .values(last_activity=_now() - 999999)
    )

    rows = store.list_recent_dead(limit=1, exclude_session_ids={"held"})
    assert len(rows) == 1
    assert rows[0].id == allowed.id


def test_spawn_prompts_for_targets_returns_first_spawn_prompt(store):
    p = Participant(harness="vibe", cwd="/tmp")
    store.upsert_participant(p)
    store.create_job(
        Job(
            handle="spawn1",
            caller_id="cli",
            target_id=p.id,
            kind="spawn",
            prompt="first task",
            state=JobState.RUNNING,
            result=None,
            error_code=None,
            created_at=1.0,
            finished_at=None,
        )
    )
    prompts = store.spawn_prompts_for_targets([p.id])
    assert prompts == {p.id: "first task"}


def test_spawn_prompts_for_targets_empty_ids_returns_empty(store):
    assert store.spawn_prompts_for_targets([]) == {}


def test_spawn_prompts_for_targets_missing_target_returns_empty(store):
    assert store.spawn_prompts_for_targets(["nonexistent"]) == {}
