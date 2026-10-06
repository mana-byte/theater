"""Live participant names survive a daemon restart; dead participants keep none."""

from __future__ import annotations

from sqlalchemy import update

from theater.daemon.gc import _delete_participant_dependents
from theater.daemon.registry import Registry
from theater.daemon.schema import participant_names, participants
from theater.models import Participant, Status


def _restart(registry: Registry) -> Registry:
    """A new daemon on the same database: construction loads, persist_names() writes."""
    restarted = Registry(registry.store)
    restarted.persist_names()
    return restarted


def _stored_ids(registry: Registry) -> set[str]:
    rows = registry.store.conn.execute(participant_names.select()).fetchall()
    return {row.participant_id for row in rows}


def _events(registry: Registry) -> int:
    return registry.store.journal.current_sequence()


def test_names_survive_a_restart_and_dead_participants_release_theirs(registry):
    auto = registry.register(harness="vibe", pane=None, cwd="/tmp")
    renamed = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.rename(renamed.id, "Scapin")
    with registry.store.write_unit() as unit:
        reserved = registry.create_spawned(harness="vibe", cwd="/tmp", connection=unit.connection)
    registry.remember_reserved_name(reserved.id, reserved.name)
    killed = registry.register(harness="vibe", pane=None, cwd="/tmp")
    killed_name = killed.name
    # A death that bypasses mark_dead (bulk tmux restart): the row is left behind.
    registry.store.conn.execute(
        update(participants).where(participants.c.id == killed.id).values(status="dead")
    )

    died = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.mark_dead(died.id)
    assert died.id not in _stored_ids(registry)  # mark_dead drops the row at once

    restarted = _restart(registry)

    assert [restarted.get(p.id).name for p in (auto, renamed, reserved)] == [
        auto.name,
        "Scapin",
        reserved.name,
    ]
    assert restarted.projection_name(killed.id) is None
    assert restarted.rename(auto.id, killed_name).name == killed_name  # the mask is free again
    assert killed.id not in _stored_ids(registry)  # persist_names() purged the leftover row


def test_a_legacy_row_is_named_once_and_journaled_once(registry):
    legacy = Participant(id="1" * 12, harness="vibe", status=Status.IDLE)
    registry.store.upsert_participant(legacy)  # stored without a name, as before the upgrade
    before = _events(registry)

    first = _restart(registry)
    name = first.get(legacy.id).name
    after_first = _events(registry)
    second = _restart(registry)

    assert name is not None and second.get(legacy.id).name == name
    assert after_first == before + 1
    assert _events(registry) == after_first  # a normal restart journals nothing


def test_duplicate_stored_names_resolve_without_raising(registry):
    older = registry.register(harness="vibe", pane=None, cwd="/tmp")
    newer = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.store.conn.execute(  # distinct ages, so "oldest wins" never falls back to the id
        update(participants)
        .where(participants.c.id == newer.id)
        .values(created_at=older.created_at + 1)
    )
    registry.store.set_participant_name(older.id, "Pierrot")
    registry.store.set_participant_name(newer.id, "pierrot")

    restarted = _restart(registry)

    assert restarted.get(older.id).name == "Pierrot"
    assert restarted.get(newer.id).name not in {"pierrot", "Pierrot"}
    assert dict(registry.store.live_participant_names())[newer.id] == restarted.get(newer.id).name


def test_deleting_a_dead_participant_removes_its_name_row(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.store.conn.execute(
        update(participants).where(participants.c.id == p.id).values(status="dead")
    )
    _delete_participant_dependents(registry.store.conn, p.id)
    assert p.id not in _stored_ids(registry)
