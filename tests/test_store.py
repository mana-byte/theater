from __future__ import annotations

from theater.models import Job, JobState, Participant, Status, Tier, now


def test_roundtrip(store):
    p = Participant(harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1", cwd="/tmp")
    store.upsert_participant(p)

    got = store.get_participant(p.id)
    assert got is not None
    assert got.harness == "vibe"
    assert got.tier is Tier.SPAWNED
    assert got.tmux_pane == "%1"


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


def test_reopening_does_not_wipe_state(store, theater_home):
    from theater import paths
    from theater.daemon.store import Store

    p = Participant(
        harness="vibe",
        session_id="native-session",
        session_correlation="heuristic",
        transcript_domain="/tmp/vibe-root",
        transcript_location="/tmp/vibe-root/session/messages.jsonl",
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
    again.close()


def test_a_persisted_starting_status_loads_as_idle(store):
    """A database from an older daemon may contain the 'starting' value.

    The STARTING status was removed, so a row with the old value must load
    as IDLE rather than raising ValueError. This is the migration shim in
    Participant.from_row.
    """
    from theater.daemon.store import participants

    p = Participant(harness="vibe", tier=Tier.SPAWNED)
    store.upsert_participant(p)

    # Write the old value directly, bypassing the enum, as an upgrade would.
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


# ---- Job structured fields ------------------------------------------------


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


# ---- scratchpad -------------------------------------------------------------


def test_scratchpad_write_and_get(store):
    key = store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="val1",
        updated_by="p1",
    )
    assert isinstance(key, str) and len(key) > 0
    entries = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    assert entries == {key: "val1"}


def test_scratchpad_get_missing_returns_empty(store):
    entries = store.scratchpad_get(tree_root_id="nope", repo_root="/repo", namespace="ns1")
    assert entries == {}


def test_scratchpad_write_with_key_updates_existing(store):
    key = store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="old",
        updated_by="p1",
    )
    same = store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="new",
        updated_by="p2",
        key=key,
    )
    assert same == key
    entries = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    assert entries == {key: "new"}


def test_scratchpad_write_with_nonexistent_key_inserts(store):
    key = store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="val",
        updated_by="p1",
        key="my-custom-key",
    )
    assert key == "my-custom-key"
    entries = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    assert entries == {"my-custom-key": "val"}


def test_scratchpad_get_with_keys_filters(store):
    a = store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="va",
        updated_by="p1",
    )
    b = store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="vb",
        updated_by="p1",
    )
    filtered = store.scratchpad_get(
        tree_root_id="root1", repo_root="/repo", namespace="ns1", keys=[a]
    )
    assert filtered == {a: "va"}
    all_entries = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    assert set(all_entries.keys()) == {a, b}


def test_scratchpad_isolation_by_tree(store):
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="from-root1",
        updated_by="p1",
    )
    store.scratchpad_write(
        tree_root_id="root2",
        repo_root="/repo",
        namespace="ns1",
        value="from-root2",
        updated_by="p2",
    )
    r1 = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    r2 = store.scratchpad_get(tree_root_id="root2", repo_root="/repo", namespace="ns1")
    assert list(r1.values()) == ["from-root1"]
    assert list(r2.values()) == ["from-root2"]


def test_scratchpad_isolation_by_namespace(store):
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="from-ns1",
        updated_by="p1",
    )
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns2",
        value="from-ns2",
        updated_by="p1",
    )
    assert list(
        store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1").values()
    ) == ["from-ns1"]
    assert list(
        store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns2").values()
    ) == ["from-ns2"]


def test_scratchpad_isolation_by_repo_root(store):
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo-a",
        namespace="ns1",
        value="from-a",
        updated_by="p1",
    )
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo-b",
        namespace="ns1",
        value="from-b",
        updated_by="p1",
    )
    assert list(
        store.scratchpad_get(tree_root_id="root1", repo_root="/repo-a", namespace="ns1").values()
    ) == ["from-a"]
    assert list(
        store.scratchpad_get(tree_root_id="root1", repo_root="/repo-b", namespace="ns1").values()
    ) == ["from-b"]


# ---- checkpoints -----------------------------------------------------------


def test_checkpoint_create_and_get(store):
    cid = store.create_checkpoint(
        participant_id="p1",
        name="plan-a",
        jobs_snapshot='[{"handle":"j1"}]',
        notes="initial",
    )
    assert isinstance(cid, int)
    row = store.get_checkpoint(cid)
    assert row is not None
    assert row["participant_id"] == "p1"
    assert row["name"] == "plan-a"
    assert row["jobs_snapshot"] == '[{"handle":"j1"}]'
    assert row["notes"] == "initial"


def test_checkpoint_get_missing_returns_none(store):
    assert store.get_checkpoint(99999) is None


def test_checkpoint_list_newest_first(store):
    for i in range(3):
        cid = store.create_checkpoint(
            participant_id="p1",
            name=f"plan-{i}",
            jobs_snapshot="[]",
        )
        # Ensure distinct created_at timestamps so ordering is deterministic.
        from sqlalchemy import update as sa_update

        from theater.daemon.schema import checkpoints as ckpt_table

        store.conn.execute(
            sa_update(ckpt_table).where(ckpt_table.c.id == cid).values(created_at=float(1000 + i))
        )
    rows = store.list_checkpoints(participant_id="p1")
    assert [r["name"] for r in rows] == ["plan-2", "plan-1", "plan-0"]


def test_checkpoint_list_filters_by_participant_when_given(store):
    store.create_checkpoint(participant_id="p1", name="a", jobs_snapshot="[]")
    store.create_checkpoint(participant_id="p2", name="b", jobs_snapshot="[]")
    rows = store.list_checkpoints(participant_id="p1")
    assert len(rows) == 1
    assert rows[0]["name"] == "a"


def test_checkpoint_list_global_returns_all_participants(store):
    store.create_checkpoint(participant_id="p1", name="a", jobs_snapshot="[]")
    store.create_checkpoint(participant_id="p2", name="b", jobs_snapshot="[]")
    rows = store.list_checkpoints()
    names = {r["name"] for r in rows}
    assert names == {"a", "b"}


def test_checkpoint_notes_are_optional(store):
    cid = store.create_checkpoint(participant_id="p1", name="plan", jobs_snapshot="[]")
    row = store.get_checkpoint(cid)
    assert row is not None
    assert row["notes"] is None


def test_checkpoint_list_global_newest_first_across_creators(store):
    from sqlalchemy import update as sa_update

    from theater.daemon.schema import checkpoints as ckpt_table

    cid1 = store.create_checkpoint(participant_id="p1", name="alpha", jobs_snapshot="[]")
    cid2 = store.create_checkpoint(participant_id="p2", name="beta", jobs_snapshot="[]")
    store.conn.execute(sa_update(ckpt_table).where(ckpt_table.c.id == cid1).values(created_at=1.0))
    store.conn.execute(sa_update(ckpt_table).where(ckpt_table.c.id == cid2).values(created_at=2.0))
    rows = store.list_checkpoints()
    assert [r["name"] for r in rows] == ["beta", "alpha"]


def test_checkpoint_list_limit_applies_across_creators(store):
    for i in range(4):
        store.create_checkpoint(participant_id=f"p{i % 2}", name=f"cp-{i}", jobs_snapshot="[]")
    rows = store.list_checkpoints(limit=2)
    assert len(rows) == 2


def test_checkpoint_finalize_restore_failure_records_restored_by(store):
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="[]")
    token = store.claim_checkpoint_restore(cid, "restorer-x")
    assert token is not None
    ok = store.finalize_checkpoint_restore(
        cid, token=token, restored_by="restorer-x", error="exploded"
    )
    assert ok is True
    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "failed"
    assert row["restored_by"] == "restorer-x"
    assert row["restore_error"] == "exploded"


def test_checkpoint_claim_records_restore_claimed_by(store):
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="[]")
    token = store.claim_checkpoint_restore(cid, "claimer-1")
    assert token is not None
    row = store.get_checkpoint(cid)
    assert row["restore_claimed_by"] == "claimer-1"


def test_checkpoint_release_clears_restore_claimed_by(store):
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="[]")
    token = store.claim_checkpoint_restore(cid, "claimer-1")
    assert token is not None
    ok = store.release_checkpoint_restore(cid, token=token)
    assert ok is True
    row = store.get_checkpoint(cid)
    assert row["restore_claimed_by"] is None
    assert row["restore_state"] == "ready"


def test_recover_stranded_restores_promotes_claimed_by_to_restored_by(store):
    """recover_stranded_restores must converge on the same row shape as finalize(error=...).

    Specifically: restored_by is set to the claimant (via column-to-column copy
    in a single UPDATE statement) and restore_claimed_by is cleared to NULL.
    This verifies that SQLite evaluates the right-hand side against the
    pre-update row, so both can be set correctly in one statement.
    """
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="[]")
    token = store.claim_checkpoint_restore(cid, "crash-restorer")
    assert token is not None

    count = store.recover_stranded_restores()
    assert count == 1

    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "failed"
    assert row["restore_error"] == "daemon restarted while restore was in progress"
    # The claimant must be promoted to restored_by, not lost.
    assert row["restored_by"] == "crash-restorer"
    # restore_claimed_by must be cleared — same shape as finalize(error=...).
    assert row["restore_claimed_by"] is None


# ---- list_participants ids filter -----------------------------------------


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
