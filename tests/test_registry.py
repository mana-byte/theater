from __future__ import annotations

import pytest

from theater.models import BadRequest, NameTaken, NotFound, Status, Tier


def test_pane_makes_you_adopted(registry):
    p = registry.register(harness="vibe", pane="%3", cwd="/tmp")
    assert p.tier is Tier.ADOPTED
    assert p.addressable


def test_no_pane_makes_you_external_and_unreachable(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    assert p.tier is Tier.EXTERNAL
    assert not p.addressable


def test_spawned_identity_survives_hello(registry):
    created = registry.create_spawned(harness="vibe", cwd="/tmp")
    registry.attach_pane(created.id, "%9")

    # The MCP server comes up later and presents the id it was given on argv.
    seen = registry.register(harness="vibe", pane=None, cwd="/tmp", claimed_id=created.id)
    assert seen.id == created.id
    assert seen.tier is Tier.SPAWNED
    assert seen.tmux_pane == "%9"
    assert seen.status is Status.IDLE


def test_an_unknown_claimed_id_is_honoured_not_refused(registry):
    """A daemon restart must not orphan a live agent."""
    p = registry.register(harness="vibe", pane="%4", cwd="/tmp", claimed_id="ghost123")
    assert p.id == "ghost123"
    assert p.tier is Tier.ADOPTED


def test_reconnecting_on_the_same_pane_reuses_the_record(registry):
    first = registry.register(harness="vibe", pane="%5", cwd="/tmp")
    again = registry.register(harness="vibe", pane="%5", cwd="/tmp")
    assert first.id == again.id
    assert len(registry.list()) == 1


def test_a_dead_pane_does_not_capture_its_successor(registry):
    first = registry.register(harness="vibe", pane="%6", cwd="/tmp")
    registry.mark_dead(first.id)

    second = registry.register(harness="claude", pane="%6", cwd="/tmp")
    assert second.id != first.id


def test_a_late_pane_report_promotes_an_external(registry):
    """The adoption fallback: an agent finds its own $TMUX_PANE and calls back.

    This is the primary way anyone becomes addressable, because the MCP SDK's
    environment allowlist hides TMUX_PANE from the server process itself.
    """
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    assert p.tier is Tier.EXTERNAL

    promoted = registry.register(harness="vibe", pane="%42", cwd="/tmp", claimed_id=p.id)
    assert promoted.id == p.id
    assert promoted.tier is Tier.ADOPTED
    assert promoted.tmux_pane == "%42"
    assert promoted.addressable


def test_a_pane_less_hello_does_not_demote_an_adopted(registry):
    """Absence of a pane is not evidence of losing one.

    Every routine whoami sends pane=None, so demoting on a missing pane would
    undo adoption on the very next call.
    """
    p = registry.register(harness="vibe", pane="%7", cwd="/tmp")
    again = registry.register(harness="vibe", pane=None, cwd="/tmp", claimed_id=p.id)

    assert again.tier is Tier.ADOPTED
    assert again.tmux_pane == "%7"


def test_a_spawned_child_cannot_talk_its_way_into_another_pane(registry):
    """We watched tmux create that pane. A self-report does not outrank that."""
    child = registry.create_spawned(harness="vibe", cwd="/tmp")
    registry.attach_pane(child.id, "%9")

    lying = registry.register(harness="vibe", pane="%99", cwd="/tmp", claimed_id=child.id)
    assert lying.tmux_pane == "%9"
    assert lying.tier is Tier.SPAWNED


def test_one_pane_has_one_holder(registry):
    """Otherwise a delivery could be typed into the wrong agent's terminal.

    tmux does not recycle pane ids, so a second claimant means the user quit one
    agent and started another in the same seat.
    """
    old = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.register(harness="vibe", pane="%11", cwd="/tmp", claimed_id=old.id)

    new = registry.register(harness="claude", pane=None, cwd="/tmp")
    registry.register(harness="claude", pane="%11", cwd="/tmp", claimed_id=new.id)

    evicted = registry.get(old.id)
    assert evicted.tmux_pane is None
    assert evicted.status is Status.DEAD
    assert not evicted.addressable
    assert registry.get(new.id).tmux_pane == "%11"


def test_tree_nests_children(registry):
    root = registry.create_spawned(harness="vibe", cwd="/tmp")
    child = registry.create_spawned(harness="claude", cwd="/tmp", parent_id=root.id)

    tree = registry.tree()
    assert len(tree) == 1
    assert tree[0]["id"] == root.id
    assert [c["id"] for c in tree[0]["children"]] == [child.id]


def test_get_missing_raises(registry):
    with pytest.raises(NotFound):
        registry.get("nope")


# ---- runtime names --------------------------------------------------------


def test_a_fresh_participant_gets_a_name(registry):
    p = registry.register(harness="vibe", pane="%3", cwd="/tmp")
    assert p.name is not None
    assert len(p.name) >= 4


def test_name_is_stable_across_repeated_get(registry):
    p = registry.register(harness="vibe", pane="%3", cwd="/tmp")
    again = registry.get(p.id)
    assert again.name == p.name


def test_two_participants_never_share_a_name(registry):
    a = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    b = registry.register(harness="vibe", pane="%2", cwd="/tmp")
    assert a.name != b.name


def test_name_survives_attach_pane(registry):
    p = registry.create_spawned(harness="vibe", cwd="/tmp")
    name_before = p.name
    attached = registry.attach_pane(p.id, "%9")
    assert attached.name == name_before


def test_rename_rejects_a_taken_name(registry):
    a = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    b = registry.register(harness="vibe", pane="%2", cwd="/tmp")
    with pytest.raises(NameTaken):
        registry.rename(b.id, a.name)


def test_rename_to_own_current_name_succeeds(registry):
    p = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    result = registry.rename(p.id, p.name)
    assert result.name == p.name


def test_rename_with_only_casing_changed_is_applied(registry):
    p = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    upper = p.name.upper()
    result = registry.rename(p.id, upper)
    assert result.name == upper


def test_rename_rejects_a_hex_string_id(registry):
    p = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    with pytest.raises(BadRequest):
        registry.rename(p.id, "deadbeefcafe")


def test_rename_rejects_a_name_with_a_space(registry):
    p = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    with pytest.raises(BadRequest):
        registry.rename(p.id, "bad name")


def test_resolve_finds_by_id(registry):
    p = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    found = registry.resolve(p.id)
    assert found.id == p.id


def test_resolve_finds_by_exact_name(registry):
    p = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    found = registry.resolve(p.name)
    assert found.id == p.id


def test_resolve_finds_by_differently_cased_name(registry):
    p = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    found = registry.resolve(p.name.upper())
    assert found.id == p.id


def test_resolve_raises_for_a_stranger(registry):
    with pytest.raises(NotFound):
        registry.resolve("nobody")


def test_resolve_returns_participant_with_name(registry):
    p = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    found = registry.resolve(p.id)
    assert found.name is not None


def test_rename_changes_the_name(registry):
    p = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    old_name = p.name
    renamed = registry.rename(p.id, "Truffaldino")
    assert renamed.name == "Truffaldino"
    # The old name is freed and the new one resolves.
    assert registry.resolve("Truffaldino").id == p.id
    # Old name no longer resolves to this participant.
    with pytest.raises(NotFound):
        registry.resolve(old_name)


def test_rename_by_current_name(registry):
    p = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    renamed = registry.rename(p.name, "Scapino")
    assert renamed.id == p.id
    assert renamed.name == "Scapino"
