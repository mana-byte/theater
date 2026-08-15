from __future__ import annotations

import pytest

from theater.models import NotFound, Status, Tier


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
