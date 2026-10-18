"""participants.list and participants.tree over RPC: filters, paging and resume state."""

from __future__ import annotations

import pytest

from theater.models import Participant, Status
from theater.protocol import RemoteError


async def test_lineage_shows_in_the_tree(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child1 = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    child2 = await client.call(
        "spawn",
        harness="claude",
        prompt="hi again",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    tree = await client.call("participants.tree")
    assert len(tree) == 1
    assert tree[0]["id"] == parent["id"]
    assert {c["id"] for c in tree[0]["children"]} == {child1["id"], child2["id"]}


async def test_list_ids_omitted_returns_all(client):
    """ids omitted => response identical to today (all live rows)."""
    a = await client.call("hello", harness="vibe", cwd="/tmp")
    b = await client.call("hello", harness="vibe", cwd="/tmp")
    rows = await client.call("participants.list")
    ids = [r["id"] for r in rows]
    assert a["id"] in ids
    assert b["id"] in ids


async def test_list_ids_subset_returns_exact_rows(client):
    """ids=[a, c] out of several participants => exactly those rows."""
    a = await client.call("hello", harness="vibe", cwd="/tmp")
    await client.call("hello", harness="vibe", cwd="/tmp")
    c = await client.call("hello", harness="vibe", cwd="/tmp")
    rows = await client.call("participants.list", ids=[a["id"], c["id"]])
    assert [r["id"] for r in rows] == [a["id"], c["id"]]


async def test_list_ids_empty_returns_nothing(client):
    """ids=[] is the trap: must return [] not everything."""
    await client.call("hello", harness="vibe", cwd="/tmp")
    rows = await client.call("participants.list", ids=[])
    assert rows == []


async def test_list_ids_unknown_silently_omitted(client):
    """Unknown ids are dropped, no error."""
    rows = await client.call("participants.list", ids=["ghost-123"])
    assert rows == []


async def test_list_ids_not_a_list_is_bad_request(client):
    """ids must be a list."""
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", ids="abc")
    assert exc.value.code == "bad_request"


async def test_list_ids_element_not_string_is_bad_request(client):
    """Any non-string element is rejected."""
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", ids=[1])
    assert exc.value.code == "bad_request"


async def test_list_ids_empty_string_element_is_bad_request(client):
    """An empty string element must not silently widen the query."""
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", ids=[""])
    assert exc.value.code == "bad_request"


async def test_list_ids_over_200_is_bad_request(client):
    """More than 200 ids at once is refused."""
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", ids=[f"x-{i}" for i in range(201)])
    assert exc.value.code == "bad_request"


async def test_list_ids_dead_excluded_without_include_dead(client, terminal_provider):
    """A dead id is omitted when include_dead=False, even when named explicitly."""
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("participant.kill", id=record["id"])
    rows = await client.call("participants.list", ids=[record["id"]])
    assert rows == []


async def test_list_ids_dead_returned_with_include_dead(client, terminal_provider):
    """A dead id is returned when include_dead=True."""
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("participant.kill", id=record["id"])
    rows = await client.call("participants.list", ids=[record["id"]], include_dead=True)
    assert len(rows) == 1
    assert rows[0]["id"] == record["id"]
    assert rows[0]["status"] == "dead"


async def test_list_live_default_remains_unbounded(client, daemon):
    for index in range(101):
        daemon.store.upsert_participant(
            Participant(id=f"live-{index:03}", harness="vibe", created_at=float(index))
        )

    rows = await client.call("participants.list")
    assert [row["id"] for row in rows] == [f"live-{index:03}" for index in range(101)]


async def test_list_include_dead_is_unbounded_without_explicit_limit(client, daemon):
    for index in range(101):
        daemon.store.upsert_participant(
            Participant(
                id=f"dead-{index:03}",
                harness="vibe",
                status=Status.DEAD,
                created_at=float(index),
            )
        )

    all_rows = await client.call("participants.list", include_dead=True)
    first = await client.call("participants.list", include_dead=True, limit=100)
    second = await client.call(
        "participants.list",
        include_dead=True,
        limit=100,
        after_id=first[-1]["id"],
    )
    end = await client.call(
        "participants.list",
        include_dead=True,
        limit=100,
        after_id=second[-1]["id"],
    )

    assert [row["id"] for row in all_rows] == [f"dead-{index:03}" for index in range(101)]
    assert [row["id"] for row in first] == [f"dead-{index:03}" for index in range(100)]
    assert [row["id"] for row in second] == ["dead-100"]
    assert end == []


@pytest.mark.parametrize("limit", [True, "10", 0, 201])
async def test_list_rejects_invalid_page_limit(client, limit):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", limit=limit)
    assert exc.value.code == "bad_request"


@pytest.mark.parametrize("after_id", ["", 1])
async def test_list_rejects_invalid_after_id(client, after_id):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", after_id=after_id)
    assert exc.value.code == "bad_request"


async def test_list_rejects_missing_keyset_cursor(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", after_id="gone")
    assert exc.value.code == "bad_request"
    assert "restart pagination" in str(exc.value)


async def test_list_rejects_a_cursor_with_ids(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", ids=[], after_id="p-any")
    assert exc.value.code == "bad_request"


async def test_list_newest_first_pages_backwards_and_caps_ids(client, daemon):
    for index in range(5):
        daemon.store.upsert_participant(
            Participant(
                id=f"dead-{index}", harness="vibe", status=Status.DEAD, created_at=float(index)
            )
        )
    first = await client.call("participants.list", include_dead=True, order="newest", limit=2)
    second = await client.call(
        "participants.list",
        include_dead=True,
        order="newest",
        limit=2,
        after_id=first[-1]["id"],
    )
    capped = await client.call(
        "participants.list", include_dead=True, ids=["dead-0", "dead-1", "dead-2"], limit=2
    )
    assert [row["id"] for row in first] == ["dead-4", "dead-3"]
    assert [row["id"] for row in second] == ["dead-2", "dead-1"]
    assert [row["id"] for row in capped] == ["dead-0", "dead-1"]


async def test_list_keyset_composes_with_direct_children_and_dead_rows(client, daemon):
    parent = Participant(id="parent", harness="vibe", created_at=0.0)
    child_a = Participant(
        id="child-a",
        harness="vibe",
        parent_id=parent.id,
        status=Status.DEAD,
        created_at=1.0,
    )
    child_b = Participant(
        id="child-b",
        harness="vibe",
        parent_id=parent.id,
        status=Status.DEAD,
        created_at=2.0,
    )
    outsider = Participant(id="outsider", harness="vibe", status=Status.DEAD, created_at=3.0)
    for participant in (parent, child_a, child_b, outsider):
        daemon.store.upsert_participant(participant)

    first = await client.call(
        "participants.list",
        include_dead=True,
        parent_id=parent.id,
        limit=1,
    )
    second = await client.call(
        "participants.list",
        include_dead=True,
        parent_id=parent.id,
        limit=1,
        after_id=first[-1]["id"],
    )

    assert [row["id"] for row in first] == [child_a.id]
    assert [row["id"] for row in second] == [child_b.id]


async def test_list_parent_filter_returns_direct_children_only(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="child",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    grandchild = await client.call(
        "spawn",
        harness="vibe",
        prompt="grandchild",
        approval="manual",
        cwd="/tmp",
        parent_id=child["id"],
    )
    await client.call("hello", harness="vibe", cwd="/tmp")

    rows = await client.call("participants.list", parent_id=parent["id"])
    assert [row["id"] for row in rows] == [child["id"]]
    assert grandchild["id"] not in {row["id"] for row in rows}


async def test_list_parent_filter_composes_with_ids_and_include_dead(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="child",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    sibling = await client.call(
        "spawn",
        harness="vibe",
        prompt="sibling",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    outsider = await client.call(
        "spawn",
        harness="vibe",
        prompt="outsider",
        approval="manual",
        cwd="/tmp",
    )
    await client.call("participant.kill", id=child["id"])

    live_rows = await client.call(
        "participants.list",
        parent_id=parent["id"],
        ids=[child["id"], sibling["id"], outsider["id"]],
    )
    assert [row["id"] for row in live_rows] == [sibling["id"]]

    all_rows = await client.call(
        "participants.list",
        parent_id=parent["id"],
        ids=[child["id"], sibling["id"], outsider["id"]],
        include_dead=True,
    )
    assert [row["id"] for row in all_rows] == [child["id"], sibling["id"]]


async def test_list_resume_state_live(client):
    """A live participant reports resume_state == 'live'."""
    me = await client.call("hello", harness="vibe", cwd="/tmp")
    rows = await client.call("participants.list")
    row = next(r for r in rows if r["id"] == me["id"])
    assert row["resume_state"] == "live"


async def test_list_resume_state_no_session_id(client, terminal_provider):
    """Dead participant with no session_id => no_session_id."""
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    pid = record["id"]
    await client.call("participant.kill", id=pid)
    rows = await client.call("participants.list", include_dead=True)
    row = next(r for r in rows if r["id"] == pid)
    # session_id is not set by the fake tmux observer, so it remains None.
    assert row["resume_state"] == "no_session_id"


async def test_list_resume_state_untrusted(client, daemon):
    """Dead, has session_id, but heuristic provenance => untrusted."""
    p = await client.call("hello", harness="vibe", cwd="/tmp")
    pid = p["id"]
    participant = daemon.registry.get(pid)
    participant.session_id = "sess-heuristic"
    participant.session_correlation = "heuristic"
    daemon.store.upsert_participant(participant)
    daemon.registry.mark_dead(pid)

    rows = await client.call("participants.list", include_dead=True)
    row = next(r for r in rows if r["id"] == pid)
    assert row["resume_state"] == "untrusted"


async def test_list_resume_state_resumable(client, daemon):
    """Dead, has session_id, trusted provenance, no live owner => resumable."""
    p = await client.call("hello", harness="vibe", cwd="/tmp")
    pid = p["id"]
    participant = daemon.registry.get(pid)
    participant.session_id = "sess-trusted"
    participant.session_correlation = "operator"
    daemon.store.upsert_participant(participant)
    daemon.registry.mark_dead(pid)

    rows = await client.call("participants.list", include_dead=True)
    row = next(r for r in rows if r["id"] == pid)
    assert row["resume_state"] == "resumable"


async def test_list_resume_state_owned_by_live(client, daemon):
    """Dead trusted row AND live trusted row sharing session id => owned_by_live."""
    # Dead participant with trusted binding.
    dead_p = await client.call("hello", harness="vibe", cwd="/tmp")
    dead_id = dead_p["id"]
    dead_part = daemon.registry.get(dead_id)
    dead_part.session_id = "sess-shared"
    dead_part.session_correlation = "operator"
    dead_part.created_at = 0.0
    daemon.store.upsert_participant(dead_part)
    daemon.registry.mark_dead(dead_id)

    # Live participant with the same harness + session_id at trusted provenance.
    live_p = await client.call("hello", harness="vibe", cwd="/tmp")
    live_id = live_p["id"]
    live_part = daemon.registry.get(live_id)
    live_part.session_id = "sess-shared"
    live_part.session_correlation = "operator"
    daemon.store.upsert_participant(live_part)

    rows = await client.call("participants.list", include_dead=True, limit=1)
    assert [row["id"] for row in rows] == [dead_id]
    dead_row = rows[0]
    assert dead_row["resume_state"] == "owned_by_live"


async def test_list_resume_state_owned_by_live_beats_untrusted(client, daemon):
    """An untrusted dead row with a trusted live peer reports owned_by_live, not untrusted.

    The spawner's _validate_resume_identity filters to trusted participants only,
    so the untrusted dead row is invisible to it.  The live trusted peer triggers
    the live-owner gate regardless of the subject row's own provenance.  This
    test is the key regression guard for the precedence inversion bug.
    """
    # Dead participant with UNTRUSTED provenance.
    dead_p = await client.call("hello", harness="vibe", cwd="/tmp")
    dead_id = dead_p["id"]
    dead_part = daemon.registry.get(dead_id)
    dead_part.session_id = "sess-mixed"
    dead_part.session_correlation = "heuristic"  # untrusted
    daemon.store.upsert_participant(dead_part)
    daemon.registry.mark_dead(dead_id)

    # Live participant with the same harness + session_id at TRUSTED provenance.
    live_p = await client.call("hello", harness="vibe", cwd="/tmp")
    live_id = live_p["id"]
    live_part = daemon.registry.get(live_id)
    live_part.session_id = "sess-mixed"
    live_part.session_correlation = "operator"
    daemon.store.upsert_participant(live_part)

    rows = await client.call("participants.list", include_dead=True)
    dead_row = next(r for r in rows if r["id"] == dead_id)
    # Must be owned_by_live, not untrusted.
    assert dead_row["resume_state"] == "owned_by_live"


async def test_list_no_internal_fields_exposed(client):
    """Internal observation fields must not appear in participants.list."""
    await client.call("hello", harness="vibe", cwd="/tmp")
    rows = await client.call("participants.list")
    for row in rows:
        assert "session_correlation" not in row
        assert "transcript_domain" not in row
        assert "transcript_location" not in row
        assert "resume_floor" not in row
        assert "source_checkpoint" not in row
