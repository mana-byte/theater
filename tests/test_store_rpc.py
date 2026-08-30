"""Daemon RPC tests for the tree-scoped scratchpad."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from theater.daemon.rpc import usage as usage_mod
from theater.daemon.schema import tree_kv
from theater.protocol import RemoteError


@pytest.fixture
def paris_timezone(monkeypatch):
    """Run local-boundary assertions outside UTC, restoring libc's timezone."""
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    yield
    if previous is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", previous)
    time.tzset()


async def test_usage_summary_rpc_returns_all_three_windows(client, monkeypatch):
    timestamp = 2_000_000.0
    monkeypatch.setattr(usage_mod, "now", lambda: timestamp)
    result = await client.call("usage_summary", window=24.0)

    assert result["since"] == timestamp - 24.0 * 3600.0
    assert set(result) == {
        "since",
        "average_since",
        "period",
        "all_time",
        "windowed",
        "average",
    }
    assert result["period"] is None
    for group in ("all_time", "windowed"):
        assert result[group] == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "reasoning_output_tokens": 0,
            "cost_microcents": 0,
        }
    assert result["average"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "cost_microcents": 0,
        "active_days": 0,
    }


async def test_participant_update_is_scoped_atomic_and_bounded(client, daemon):
    parent = await client.call("hello", id="parent", harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(
        harness="vibe",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    sibling = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    original_name = child.name

    with pytest.raises(RemoteError) as exc:
        await client.call("participant.update", caller_id=parent["id"])
    assert exc.value.code == "bad_request"

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "participant.update",
            caller_id=parent["id"],
            target=child.id,
            name="Metadata-Child",
            description="bad\nsummary",
        )
    assert exc.value.code == "bad_request"
    unchanged = daemon.registry.get(child.id)
    assert (unchanged.name, unchanged.description) == (original_name, None)

    updated = await client.call(
        "participant.update",
        caller_id=parent["id"],
        target=child.name,
        name="Metadata-Child",
        description="  Implement metadata  ",
    )
    assert (updated["name"], updated["description"]) == ("Metadata-Child", "Implement metadata")
    events = [
        row
        for row in await client.call("bus.tail", limit=50)
        if row["kind"] == "participant.metadata_changed" and row["to_id"] == child.id
    ]
    assert len(events) == 1
    assert events[0]["payload"] == {"fields": ["description", "name"]}

    cleared = await client.call(
        "participant.update",
        caller_id=parent["id"],
        description="",
    )
    assert cleared["id"] == parent["id"]
    assert cleared["description"] is None

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "participant.update",
            caller_id=parent["id"],
            target=sibling.id,
            description="not yours",
        )
    assert exc.value.code == "not_your_child"

    daemon.registry.mark_dead(child.id)
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "participant.update",
            caller_id=parent["id"],
            target=child.id,
            description="too late",
        )
    assert exc.value.code == "bad_request"


async def test_usage_summary_uses_local_calendar_period_boundaries(
    client, monkeypatch, paris_timezone
):
    zone = ZoneInfo("Europe/Paris")
    timestamp = datetime(2026, 8, 21, 15, 30, tzinfo=zone).timestamp()
    monkeypatch.setattr(usage_mod, "now", lambda: timestamp)

    expected = {
        "day": datetime(2026, 8, 21, tzinfo=zone).timestamp(),
        "week": datetime(2026, 8, 17, tzinfo=zone).timestamp(),
        "month": datetime(2026, 8, 1, tzinfo=zone).timestamp(),
        "year": datetime(2026, 1, 1, tzinfo=zone).timestamp(),
    }
    for period, since in expected.items():
        result = await client.call("usage_summary", window=1.0, period=period)
        assert result["since"] == since
        assert result["period"] == period

    unknown = await client.call("usage_summary", window=2.0, period="fortnight")
    assert unknown["since"] == timestamp - 2.0 * 3600.0
    assert unknown["period"] is None


async def test_usage_summary_local_midnight_is_dst_safe(client, monkeypatch, paris_timezone):
    zone = ZoneInfo("Europe/Paris")
    # Europe/Paris changes from +01:00 to +02:00 after midnight on this date.
    timestamp = datetime(2026, 3, 29, 12, 0, tzinfo=zone).timestamp()
    monkeypatch.setattr(usage_mod, "now", lambda: timestamp)

    result = await client.call("usage_summary", window=24.0, period="day")

    assert result["since"] == datetime(2026, 3, 29, tzinfo=zone).timestamp()


async def test_usage_summary_day_resets_all_footer_totals_at_midnight(
    client, daemon, monkeypatch, paris_timezone
):
    zone = ZoneInfo("Europe/Paris")
    midnight = datetime(2026, 8, 21, tzinfo=zone).timestamp()
    timestamp = datetime(2026, 8, 21, 15, 30, tzinfo=zone).timestamp()
    monkeypatch.setattr(usage_mod, "now", lambda: timestamp)
    base = {
        "participant_id": "p1",
        "tree_root_id": "p1",
        "model": "model",
        "harness": "codex",
        "reasoning_output_tokens": 0,
    }
    assert daemon.store.record_usage(
        **base,
        usage_key="yesterday",
        ts=midnight - 1,
        input_tokens=100,
        output_tokens=200,
        cache_creation_input_tokens=300,
        cache_read_input_tokens=400,
        cost_microcents=500,
    )
    assert daemon.store.record_usage(
        **base,
        usage_key="today",
        ts=midnight + 1,
        input_tokens=1,
        output_tokens=2,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=4,
        cost_microcents=5,
    )

    result = await client.call("usage_summary", window=24.0, period="day")

    assert result["windowed"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 4,
        "reasoning_output_tokens": 0,
        "cost_microcents": 5,
    }
    assert result["all_time"]["input_tokens"] == 101
    assert result["all_time"]["cost_microcents"] == 505


async def test_usage_by_harness_covers_week_crossing_month_and_zero_fills_plugins(
    client, daemon, monkeypatch, paris_timezone
):
    zone = ZoneInfo("Europe/Paris")
    timestamp = datetime(2026, 9, 1, 15, 30, tzinfo=zone).timestamp()
    week_start = datetime(2026, 8, 31, tzinfo=zone).timestamp()
    month_start = day_start = datetime(2026, 9, 1, tzinfo=zone).timestamp()
    monkeypatch.setattr(usage_mod, "now", lambda: timestamp)
    base = {
        "participant_id": "p1",
        "tree_root_id": "p1",
        "model": "model",
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for key, harness, ts, tokens, cost in (
        ("monday", "codex", datetime(2026, 8, 31, 12, tzinfo=zone).timestamp(), 10, 100),
        ("tuesday", "codex", datetime(2026, 9, 1, 12, tzinfo=zone).timestamp(), 1, 10),
        ("plugin", "custom", datetime(2026, 9, 1, 13, tzinfo=zone).timestamp(), 2, 20),
        # A zero-token/cost row is still an active unattributed day.
        ("legacy", "unknown", datetime(2026, 9, 1, 14, tzinfo=zone).timestamp(), 0, 0),
    ):
        assert daemon.store.record_usage(
            **base,
            usage_key=key,
            harness=harness,
            ts=ts,
            input_tokens=tokens,
            output_tokens=tokens * 2,
            cost_microcents=cost,
        )

    result = await client.call("usage_by_harness")

    assert result["since"] == {
        "day": day_start,
        "week": week_start,
        "month": month_start,
    }
    rows = result["harnesses"]
    names = [row["harness"] for row in rows]
    assert names[-2:] == ["custom", "unknown"]
    codex = rows[names.index("codex")]
    assert codex["today"]["input_tokens"] == 1
    assert codex["week"]["input_tokens"] == 11
    assert codex["week"]["active_days"] == 2
    assert codex["month"]["input_tokens"] == 1
    vibe = rows[names.index("vibe")]
    assert all(value == 0 for value in vibe["today"].values())
    unknown = rows[-1]
    assert unknown["today"]["active_days"] == 1
    assert sum(row["week"]["input_tokens"] for row in rows) == 13
    assert set(result) == {"since", "harnesses"}
    assert all("models" not in row for row in rows)

    detailed = await client.call("usage_by_harness", detailed=True)

    assert set(detailed) == {"since", "harnesses", "totals"}
    detailed_rows = {row["harness"]: row for row in detailed["harnesses"]}
    assert detailed_rows["vibe"]["models"] == []
    assert detailed_rows["codex"]["models"][0]["model"] == "model"
    assert detailed["totals"]["week"]["active_days"] == 2
    assert detailed["totals"]["today"]["cost_microcents"] == 30


async def test_usage_by_harness_does_not_zero_fill_plugins_that_failed_to_load(client, monkeypatch):
    monkeypatch.setattr(
        usage_mod,
        "describe",
        lambda: [
            {"name": "working", "error": None},
            {"name": "broken", "error": "failed to import"},
        ],
    )

    result = await client.call("usage_by_harness")

    assert [row["harness"] for row in result["harnesses"]] == ["working"]


def _repo(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


async def test_root_caller_can_write_and_get(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    wrote = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="plan",
        value="ship it",
    )
    assert wrote["namespace"] == "plan"
    assert isinstance(wrote["key"], str) and len(wrote["key"]) > 0

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="plan",
    )
    assert got == {"namespace": "plan", "entries": {wrote["key"]: "ship it"}}


async def test_descendants_and_siblings_share_scratchpad(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    root = daemon.registry.create_spawned(harness="vibe", cwd=str(repo), pid="root")
    child = daemon.registry.create_spawned(
        harness="vibe", cwd=str(repo), parent_id=root.id, pid="child"
    )
    sibling = daemon.registry.create_spawned(
        harness="vibe", cwd=str(repo), parent_id=root.id, pid="sibling"
    )

    wrote = await client.call(
        "scratchpad.write",
        caller_id=child.id,
        namespace="handoff",
        value="ready",
    )

    got = await client.call(
        "scratchpad.get",
        caller_id=sibling.id,
        namespace="handoff",
    )
    assert got == {"namespace": "handoff", "entries": {wrote["key"]: "ready"}}


async def test_scratchpad_is_isolated_between_trees(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    first = daemon.registry.create_spawned(harness="vibe", cwd=str(repo), pid="first")
    second = daemon.registry.create_spawned(harness="vibe", cwd=str(repo), pid="second")

    await client.call(
        "scratchpad.write",
        caller_id=first.id,
        namespace="handoff",
        value="first tree",
    )

    got = await client.call(
        "scratchpad.get",
        caller_id=second.id,
        namespace="handoff",
    )
    assert got == {"namespace": "handoff", "entries": {}}


async def test_scratchpad_is_isolated_between_repo_roots(client, daemon, tmp_path):
    repo_a = _repo(tmp_path, "repo-a")
    repo_b = _repo(tmp_path, "repo-b")
    root = daemon.registry.create_spawned(harness="vibe", cwd=str(repo_a), pid="root")
    child = daemon.registry.create_spawned(
        harness="vibe", cwd=str(repo_b), parent_id=root.id, pid="child"
    )

    await client.call(
        "scratchpad.write",
        caller_id=root.id,
        namespace="handoff",
        value="repo a",
    )

    got = await client.call(
        "scratchpad.get",
        caller_id=child.id,
        namespace="handoff",
    )
    assert got == {"namespace": "handoff", "entries": {}}


async def test_scratchpad_write_with_key_updates_existing(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    first = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="first",
    )
    second = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="second",
        key=first["key"],
    )
    assert second == {"namespace": "notes", "key": first["key"]}

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
    )
    assert got == {"namespace": "notes", "entries": {first["key"]: "second"}}


async def test_scratchpad_write_with_nonexistent_key_inserts(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    wrote = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="inserted",
        key="custom-key",
    )
    assert wrote == {"namespace": "notes", "key": "custom-key"}

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
    )
    assert got == {"namespace": "notes", "entries": {"custom-key": "inserted"}}


async def test_scratchpad_get_rejects_non_string_keys_elements(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.get",
            caller_id=caller["id"],
            namespace="notes",
            keys=[123],
        )
    assert exc.value.code == "bad_request"
    assert "list of strings" in str(exc.value)


async def test_scratchpad_get_with_keys_filters(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    a = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="a",
    )
    b = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="b",
    )

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
        keys=[a["key"]],
    )
    assert got == {"namespace": "notes", "entries": {a["key"]: "a"}}

    all_got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
    )
    assert set(all_got["entries"].keys()) == {a["key"], b["key"]}


async def test_scratchpad_get_missing_keys_returns_empty(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
        keys=["nonexistent"],
    )
    assert got == {"namespace": "notes", "entries": {}}


async def test_scratchpad_refuses_callers_outside_git(client, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(outside))

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.write",
            caller_id=caller["id"],
            namespace="notes",
            value="nope",
        )

    assert exc.value.code == "bad_request"
    assert "outside a git repository" in str(exc.value)


async def test_scratchpad_requires_existing_caller(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.write",
            caller_id="ghost",
            namespace="notes",
            value=str(repo),
        )

    assert exc.value.code == "bad_request"
    assert "existing participant" in str(exc.value)


async def test_scratchpad_write_ignores_client_supplied_updated_by(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="mine",
        updated_by="somebody-else",
    )

    row = daemon.store.conn.execute(tree_kv.select()).first()
    assert row is not None
    assert row._mapping["updated_by"] == caller["id"]


# ---- participants.recent_dead ------------------------------------------------


async def test_recent_dead_returns_dead_participants_with_spawn_prompt(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    child = await client.call(
        "spawn",
        harness="vibe",
        approval="manual",
        prompt="review the code",
        cwd=str(repo),
        tmux_session="test",
    )
    child_id = child["id"]

    p = daemon.registry.get(child_id)
    p.session_id = "test-session-123"
    daemon.store.upsert_participant(p)

    daemon.registry.mark_dead(child_id)

    rows = await client.call("participants.recent_dead", limit=20)
    matching = [r for r in rows if r["id"] == child_id]
    assert len(matching) == 1
    row = matching[0]
    assert row["status"] == "dead"
    assert row["spawn_prompt"] == "review the code"
    assert "resume_state" in row


async def test_recent_dead_empty_when_no_dead(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    rows = await client.call("participants.recent_dead")
    assert rows == []


async def test_recent_dead_rejects_invalid_limit(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.recent_dead", limit=0)
    assert exc.value.code == "bad_request"

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.recent_dead", limit=21)
    assert exc.value.code == "bad_request"


async def test_recent_dead_spawn_prompt_null_for_bare_cli(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    child = await client.call(
        "spawn",
        harness="vibe",
        approval="manual",
        prompt="",
        cwd=str(repo),
        tmux_session="test",
    )
    child_id = child["id"]

    p = daemon.registry.get(child_id)
    p.session_id = "bare-session-456"
    daemon.store.upsert_participant(p)

    daemon.registry.mark_dead(child_id)

    rows = await client.call("participants.recent_dead", limit=20)
    matching = [r for r in rows if r["id"] == child_id]
    assert len(matching) == 1
    assert matching[0]["spawn_prompt"] == ""


async def test_recent_dead_excludes_sessions_without_session_id(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    child = await client.call(
        "spawn",
        harness="vibe",
        approval="manual",
        prompt="no session id here",
        cwd=str(repo),
        tmux_session="test",
    )
    child_id = child["id"]

    daemon.registry.mark_dead(child_id)

    rows = await client.call("participants.recent_dead", limit=20)
    matching = [r for r in rows if r["id"] == child_id]
    assert matching == []
