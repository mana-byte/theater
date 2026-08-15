from __future__ import annotations

from theater.models import Participant, Status, Tier


def test_roundtrip(store):
    p = Participant(harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1", cwd="/tmp")
    store.upsert_participant(p)

    got = store.get_participant(p.id)
    assert got is not None
    assert got.harness == "vibe"
    assert got.tier is Tier.SPAWNED
    assert got.tmux_pane == "%1"


def test_upsert_is_idempotent(store):
    p = Participant(harness="vibe")
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

    p = Participant(harness="vibe")
    store.upsert_participant(p)
    store.close()

    again = Store(paths.db_path())
    assert again.get_participant(p.id) is not None
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
