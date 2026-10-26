"""Participant names: the mask list, assignment, renaming, release on death and restart."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import update

from tests.support.paths import fixture
from tests.support.presence import FakePresence
from tests.support.tables import eq_row, run_rows
from theater.daemon.gc import _delete_participant_dependents
from theater.daemon.registry import Registry
from theater.daemon.schema import participant_names, participants
from theater.models import BadRequest, NameTaken, NotFound, Participant, Status
from theater.names import MASKS, is_valid_name, pick
from theater.protocol import RemoteError


async def test_rename_over_rpc(client, terminal_provider):
    record = await client.call("hello", harness="vibe", cwd="/tmp")
    assert record["name"] is not None

    renamed = await client.call("participant.rename", id=record["id"], name="Truffaldino")
    assert renamed["name"] == "Truffaldino"

    fetched = await client.call("participants.get", id=record["id"])
    assert fetched["name"] == "Truffaldino"


async def test_rename_rejects_taken_name_over_rpc(client, terminal_provider):
    a = await client.call("hello", harness="vibe", cwd="/tmp")
    b = await client.call("hello", harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call("participant.rename", id=b["id"], name=a["name"])
    assert exc.value.code == "name_taken"
    assert exc.value.details == {"next": "omit name to get a free one"}


async def test_send_addressed_by_name_reaches_the_right_target(client, terminal_provider, daemon):
    target = await client.call("hello", harness="vibe", cwd="/tmp")
    participant = daemon.registry.get(target["id"])
    participant.session_id = "trusted-session"
    participant.session_correlation = "operator"
    daemon.store.upsert_participant(participant)
    terminal_id = terminal_provider.bind(daemon, participant.id)
    terminal_provider.screens[terminal_id] = (fixture("screens", "vibe_idle.txt")).read_text()
    job = await client.call("send", target=target["name"], prompt="hello by name")
    assert job["state"] == "running"
    assert job["target_id"] == target["id"]
    assert len(terminal_provider.deliveries) == 1
    assert terminal_provider.deliveries[0] == (terminal_id, "hello by name")


async def test_kill_addressed_by_name_puts_id_in_explicit_kills(client, terminal_provider, daemon):
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    name = record["name"]

    await client.call("participant.kill", id=name)

    assert record["id"] not in daemon._explicit_kills
    dead = await client.call("participants.get", id=record["id"])
    assert dead["status"] == "dead"
    assert dead["name"] is None


_FIXED_NAME = "Brighella"


async def test_former_name_freed_and_successor_can_claim_it(client, terminal_provider, daemon):
    """After death the former name neither resolves nor blocks a successor."""
    daemon.presence = FakePresence()
    first = await client.call("hello", harness="vibe", cwd="/tmp")
    await client.call("participant.rename", id=first["id"], name=_FIXED_NAME)
    await client.call("participant.kill", id=first["id"])

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.get", id=_FIXED_NAME)
    assert exc.value.code == "not_found"

    successor = await client.call("hello", harness="vibe", cwd="/tmp")
    renamed = await client.call("participant.rename", id=successor["id"], name=_FIXED_NAME)
    assert renamed["name"] == _FIXED_NAME

    fetched = await client.call("participants.get", id=_FIXED_NAME)
    assert fetched["id"] == successor["id"]
    assert fetched["name"] == _FIXED_NAME
    assert fetched["status"] != "dead"


async def test_status_dead_frees_name_and_emits_canonical_death_event(
    client, terminal_provider, daemon
):
    """participant.status DEAD frees the name and emits participant.dead, not participant.status."""
    daemon.presence = FakePresence()
    record = await client.call("hello", harness="vibe", cwd="/tmp")
    await client.call("participant.rename", id=record["id"], name=_FIXED_NAME)
    cursor = (await client.call("bus.tail", limit=1))[0]["id"]

    updated = await client.call("participant.status", id=record["id"], status="dead")
    assert updated["status"] == "dead"
    assert updated["name"] is None

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.get", id=_FIXED_NAME)
    assert exc.value.code == "not_found"

    events = await client.call("bus.tail", after_id=cursor)
    kinds = [e["kind"] for e in events if e.get("to_id") == record["id"]]
    assert "participant.dead" in kinds
    assert "participant.status" not in kinds


async def test_list_include_dead_returns_dead_rows_with_name_none(
    client, terminal_provider, daemon
):
    """participants.list(include_dead=True) returns dead rows with name=None."""
    daemon.presence = FakePresence()
    record = await client.call("hello", harness="vibe", cwd="/tmp")
    await client.call("participant.kill", id=record["id"])

    rows = await client.call("participants.list", include_dead=True)
    dead = [r for r in rows if r["id"] == record["id"]]
    assert len(dead) == 1
    assert dead[0]["status"] == "dead"
    assert dead[0]["name"] is None


async def test_read_transcript_by_dead_name_fails_not_found(client, terminal_provider, daemon):
    """read_transcript with a dead name fails at resolution before source access."""
    daemon.presence = FakePresence()
    record = await client.call("hello", harness="vibe", cwd="/tmp")
    await client.call("participant.rename", id=record["id"], name=_FIXED_NAME)
    await client.call("participant.kill", id=record["id"])

    with pytest.raises(RemoteError) as exc:
        await client.call("read_transcript", id=_FIXED_NAME)
    assert exc.value.code == "not_found"


def test_a_fresh_participant_gets_a_name(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    assert p.name is not None
    assert len(p.name) >= 4


def test_name_is_stable_across_repeated_get(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    again = registry.get(p.id)
    assert again.name == p.name


def test_two_participants_never_share_a_name(registry):
    a = registry.register(harness="vibe", pane=None, cwd="/tmp")
    b = registry.register(harness="vibe", pane=None, cwd="/tmp")
    assert a.name != b.name


def test_name_survives_paneless_hello(registry):
    p = registry.create_spawned(harness="vibe", cwd="/tmp")
    name_before = p.name
    seen = registry.register(harness="vibe", pane=None, cwd="/tmp", claimed_id=p.id)
    assert seen.name == name_before


def test_rename_rejects_a_taken_name(registry):
    a = registry.register(harness="vibe", pane=None, cwd="/tmp")
    b = registry.register(harness="vibe", pane=None, cwd="/tmp")
    with pytest.raises(NameTaken):
        registry.rename(b.id, a.name)


def test_name_taken_suggests_the_first_free_variant(registry):
    a = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.rename(a.id, "Scapin")
    b = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.rename(b.id, "Scapin-2")
    c = registry.register(harness="vibe", pane=None, cwd="/tmp")
    with pytest.raises(NameTaken, match="'Scapin-3' is free"):
        registry.rename(c.id, "Scapin-2")


def test_rename_to_own_current_name_succeeds(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    result = registry.rename(p.id, p.name)
    assert result.name == p.name


def test_rename_with_only_casing_changed_is_applied(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    upper = p.name.upper()
    result = registry.rename(p.id, upper)
    assert result.name == upper


@pytest.mark.parametrize(
    "name", ["deadbeefcafe", "bad name"], ids=["hex-string-id", "name-with-space"]
)
def test_rename_rejects_an_invalid_name(registry, name):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    with pytest.raises(BadRequest):
        registry.rename(p.id, name)


def test_spawned_metadata_is_normalized_and_name_is_reserved_before_a_pane(registry):
    participant = registry.create_spawned(
        harness="vibe",
        cwd="/tmp",
        name="Metadata-Child",
        description="  Focused metadata work  ",
    )

    assert participant.name == "Metadata-Child"
    assert participant.description == "Focused metadata work"
    assert registry.resolve("metadata-child").id == participant.id
    assert not [
        row
        for row in registry.store.bus_tail(limit=20)
        if row["kind"] == "participant.renamed" and row["to_id"] == participant.id
    ]
    assert Registry(registry.store).get(participant.id).name == "Metadata-Child"  # survives restart


@pytest.mark.parametrize("description", ["two\nlines", "bad\x00control", "x" * 161])
def test_spawned_description_rejects_unsafe_or_long_text(registry, description):
    with pytest.raises(BadRequest):
        registry.create_spawned(harness="vibe", cwd="/tmp", description=description)


def test_metadata_update_is_atomic_and_emits_one_bounded_event(registry):
    participant = registry.create_spawned(harness="vibe", cwd="/tmp")
    previous_name = participant.name

    with pytest.raises(BadRequest, match="one line"):
        registry.update_metadata(
            participant.id,
            name="Metadata-Child",
            description="bad\nsummary",
        )

    unchanged = registry.get(participant.id)
    assert unchanged.name == previous_name
    assert unchanged.description is None

    updated = registry.update_metadata(
        participant.id,
        name="Metadata-Child",
        description="  Explain the migration  ",
    )
    assert (updated.name, updated.description) == ("Metadata-Child", "Explain the migration")

    events = [
        row
        for row in registry.store.bus_tail(limit=20)
        if row["kind"] == "participant.metadata_changed" and row["to_id"] == participant.id
    ]
    assert len(events) == 1
    assert events[0]["payload"] == {"fields": ["description", "name"]}


def test_rename_changes_the_name(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    old_name = p.name
    # Choose a target guaranteed to differ from the randomly assigned mask.
    # "Truffaldino" is itself in the pool, so if the draw hands us that mask
    # the rename is a no-op and the old-name-resolves assertion below fails.
    # Derive a name that cannot collide with any mask in the pool.
    target = "Renamed-" + p.id[:4]
    renamed = registry.rename(p.id, target)
    assert renamed.name == target
    # The old name is freed and the new one resolves.
    assert registry.resolve(target).id == p.id
    # Old name no longer resolves to this participant.
    with pytest.raises(NotFound):
        registry.resolve(old_name)


def test_rename_by_current_name(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    # Choose a target guaranteed to differ from the randomly assigned mask,
    # so the rename is a genuine change, not a no-op rename to the same name.
    target = "Scapino"
    if p.name == target:
        target = "Scapin"
    renamed = registry.rename(p.name, target)
    assert renamed.id == p.id
    assert renamed.name == target


def test_mark_dead_releases_name_for_reuse(registry):
    """A dead participant's name is freed so a successor can take it."""
    a = registry.register(harness="vibe", pane=None, cwd="/tmp")
    fixed = "Truffaldino"
    registry.rename(a.id, fixed)
    registry.mark_dead(a.id)
    assert a.id not in registry._names

    successor = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.rename(successor.id, fixed)

    assert registry.resolve(fixed).id == successor.id


def test_dead_participant_returned_by_id_has_name_none(registry):
    """get() on a dead id returns the row but with name=None."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.mark_dead(p.id)
    dead = registry.get(p.id)
    assert dead.status is Status.DEAD
    assert dead.name is None
    assert p.id not in registry._names


def test_old_name_of_dead_participant_not_found(registry):
    """A dead participant's name no longer resolves by name lookup."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    old_name = p.name
    registry.mark_dead(p.id)
    with pytest.raises(NotFound) as raised:
        registry.resolve(old_name)
    assert "names resolve only while a participant lives" in raised.value.details["next"]
    assert "spawn result id" in raised.value.details["next"]
    assert "before '#'" in raised.value.details["next"]
    assert "list_participants(include_dead=true)" in raised.value.details["next"]
    assert registry.resolve(p.id).status is Status.DEAD


def test_include_dead_does_not_name_dead_participants(registry):
    """list(include_dead=True) returns dead rows, but they have name=None."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.mark_dead(p.id)
    everyone = registry.list(include_dead=True)
    dead = [x for x in everyone if x.id == p.id]
    assert len(dead) == 1
    assert dead[0].status is Status.DEAD
    assert dead[0].name is None


def test_set_status_dead_emits_canonical_dead_event(registry):
    """set_status(DEAD) delegates to mark_dead and emits participant.dead only."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.set_status(p.id, Status.DEAD)

    # Bus has participant.dead, not participant.status.
    bus = registry.store.bus_tail(limit=200)
    kinds = [e["kind"] for e in bus if e["to_id"] == p.id]
    assert "participant.dead" in kinds
    assert "participant.status" not in kinds

    # Name was released.
    assert p.id not in registry._names


def test_set_status_dead_on_missing_raises_not_found(registry):
    """set_status(missing, DEAD) raises NotFound, preserving prior validation."""
    with pytest.raises(NotFound):
        registry.set_status("missing", Status.DEAD)


def test_mark_dead_on_already_dead_cleans_stale_entry(registry):
    """Calling mark_dead on an already-dead participant purges stale names."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry._names[p.id] = p.name
    registry.store.set_status(p.id, Status.DEAD)
    assert p.id in registry._names

    registry.mark_dead(p.id)
    assert p.id not in registry._names


def test_mark_dead_on_missing_participant_cleans_stale_entry(registry):
    """Calling mark_dead on a missing id still purges any stale name entry."""
    ghost_id = "ghost12345"
    registry._names[ghost_id] = "Phantom"
    registry.mark_dead(ghost_id)
    assert ghost_id not in registry._names


def test_direct_store_dead_self_heals_on_read(registry):
    """A participant killed via Store.set_status gets its stale name purged on read."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    assert p.id in registry._names

    registry.store.set_status(p.id, Status.DEAD)

    fetched = registry.get(p.id)
    assert fetched.name is None
    assert p.id not in registry._names


def test_rename_dead_participant_by_id_raises_bad_request(registry):
    """Renaming a dead participant by its id is refused with BadRequest."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.mark_dead(p.id)
    with pytest.raises(BadRequest, match="dead"):
        registry.rename(p.id, "Truffaldino")


def test_revival_lazy_naming(registry):
    """A participant revived via registry.set_status gets a fresh name on read."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.mark_dead(p.id)
    assert registry.get(p.id).name is None

    registry.set_status(p.id, Status.IDLE)
    revived = registry.get(p.id)
    assert revived.name is not None
    assert revived.id in registry._names


def test_fresh_registry_with_many_dead_rows_not_exhausting_masks(registry, store):
    """A fresh Registry over a store with 100+ historical dead rows does not
    name dead rows on startup materialization and can still assign bare masks."""
    from theater import names as names_mod

    for i in range(120):
        p = registry.register(harness="vibe", pane=None, cwd=f"/tmp/{i + 1}")
        registry.mark_dead(p.id)

    fresh = Registry(store)
    everyone = fresh.list(include_dead=True)
    assert len(everyone) >= 120
    assert all(x.name is None for x in everyone if x.status is Status.DEAD)
    assert len(fresh._names) == 0

    live = fresh.register(harness="vibe", pane=None, cwd="/tmp/live")
    assert live.name in names_mod.MASKS


def test_resolve_dead_by_id_preserves_id_before_name(registry):
    """resolve() with a dead participant's id returns the dead row (name=None)."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.mark_dead(p.id)

    result = registry.resolve(p.id)
    assert result.id == p.id
    assert result.status is Status.DEAD
    assert result.name is None


def test_resolve_cleans_stale_dead_name_mapping(registry):
    """resolve() purges a stale name mapping pointing at a dead participant."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    old_name = p.name

    registry.store.set_status(p.id, Status.DEAD)
    registry._names[p.id] = old_name

    with pytest.raises(NotFound):
        registry.resolve(old_name)
    assert p.id not in registry._names


def test_non_dead_status_behaviour_unchanged(registry):
    """Setting a non-DEAD status still emits participant.status and keeps name."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    name_before = p.name
    registry.set_status(p.id, Status.WORKING)

    fetched = registry.get(p.id)
    assert fetched.status is Status.WORKING
    assert fetched.name == name_before

    bus = registry.store.bus_tail(limit=200)
    kinds = [e["kind"] for e in bus if e["to_id"] == p.id]
    assert "participant.status" in kinds
    assert "participant.dead" not in kinds


def test_resolve_stale_missing_name_mapping(registry):
    """resolve() purges a name entry pointing at an id that no longer exists."""
    ghost_id = "missing12345"
    registry._names[ghost_id] = "Phantom"
    with pytest.raises(NotFound):
        registry.resolve("Phantom")
    assert ghost_id not in registry._names


_NAME_RE = re.compile(r"^[A-Z][a-z]+$")


def test_every_mask_matches_the_capitalised_ascii_pattern():
    for m in MASKS:
        assert _NAME_RE.match(m), f"{m!r} fails ^[A-Z][a-z]+$"


def test_no_duplicates_exact_or_casefold():
    exact = list(MASKS)
    assert len(exact) == len(set(exact))
    folded = [m.casefold() for m in MASKS]
    assert len(folded) == len(set(folded))


def test_no_mask_is_pure_hex_when_casefolded():
    for m in MASKS:
        assert not all(c in "0123456789abcdef" for c in m.casefold()), f"{m!r} is pure hex"


def test_every_mask_length_is_between_4_and_12():
    for m in MASKS:
        assert 4 <= len(m) <= 12, f"{m!r} has length {len(m)}"


def test_pick_never_returns_a_taken_name():
    taken = {"Arlequin", "Pierrot", "Colombine"}
    for _ in range(50):
        name = pick(taken)
        assert name.casefold() not in {t.casefold() for t in taken}


def test_pick_returns_a_suffixed_name_when_everything_is_taken():
    taken = set(MASKS)
    name = pick(taken)
    # A suffixed name looks like ``Base-2``.
    assert "-" in name
    base, suffix = name.rsplit("-", 1)
    assert base in MASKS
    assert suffix.isdigit()
    assert name.casefold() not in {t.casefold() for t in taken}


def test_pick_is_case_insensitive_about_taken():
    taken = {"arlequin", "PIERROT"}
    for _ in range(50):
        name = pick(taken)
        assert name.casefold() not in {"arlequin", "pierrot"}


def test_name_validation_rejects_trailing_newline_and_other_control_chars():
    """Control characters never reach tmux through a participant name."""
    run_rows(
        [
            eq_row("newline", lambda: is_valid_name("Arlequin\n"), False),
            eq_row("tab", lambda: is_valid_name("Arlequin\t"), False),
            eq_row("nul", lambda: is_valid_name("Arlequin\x00"), False),
            eq_row("carriage_return", lambda: is_valid_name("Arlequin\r"), False),
        ]
    )


def test_name_validation_accepts_valid_boundaries_and_characters():
    """Length boundaries and underscore or digit suffixes are valid names."""
    run_rows(
        [
            eq_row("shortest", lambda: is_valid_name("A"), True),
            eq_row("longest", lambda: is_valid_name("a" * 24), True),
            eq_row("underscore_and_digits", lambda: is_valid_name("A_name-2"), True),
            eq_row("digit", lambda: is_valid_name("Z9"), True),
        ]
    )


def test_name_validation_rejects_invalid_names():
    """Overlong, leading-dash and spaced names are refused."""
    run_rows(
        [
            eq_row("overlong", lambda: is_valid_name("a" * 25), False),
            eq_row("leading_dash", lambda: is_valid_name("-Arlequin"), False),
            eq_row("space", lambda: is_valid_name("Arlequin space"), False),
        ]
    )


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
    # A name outside names.MASKS: register() auto-picks from MASKS at random, so a
    # mask here collides with an earlier draw ~2% of the time (xdist flake).
    registry.rename(renamed.id, "Figaro")
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
        "Figaro",
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
