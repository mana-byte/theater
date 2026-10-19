"""Typed close outcomes, retained mixed history and prerequisite visibility.

The service runs against ``FakeClient`` and ``FakeBoard``; these tests pin the
outcome-status contract: unknown is never success, one mixed history stays
canonical, reopen clears the current outcome and status, and advisory
failed_prerequisites never feed the native guards.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.beads_fakes import FakeBoard, FakeClient
from theater.mcp_plugins.builtin.beads import service
from theater.mcp_plugins.builtin.beads.backend import BeadsError
from theater.mcp_plugins.builtin.beads.service import BeadsService


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BeadsService:
    monkeypatch.setattr(service, "TheaterPluginClient", FakeClient)
    monkeypatch.setattr(service, "Board", FakeBoard)
    FakeClient.reset()
    return BeadsService(tmp_path)


def raw_row(tmp_path: Path, task_id: str) -> dict[str, Any]:
    return FakeBoard(tmp_path, "instance-one").rows()[task_id]


def edit_row(tmp_path: Path, task_id: str, mutate) -> None:
    board = FakeBoard(tmp_path, "instance-one")
    mutate(board.rows()[task_id])
    board._save()


async def make_task(adapter: BeadsService, title: str = "Task", **kwargs: Any) -> str:
    return (await adapter.create_task(title, **kwargs))["id"]


async def close_reopen(adapter: BeadsService, task_id: str, text: str, status=None) -> None:
    await adapter.close_task(task_id, text, status)
    await adapter.update_task(task_id, status="open")


async def test_close_task_refuses_unknown_status_values(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    for bad in ("done", "SUCCEEDED", "failed ", "", 5, True):
        with pytest.raises(BeadsError, match="outcome_status must be succeeded, failed"):
            await adapter.close_task(task_id, "won't close", bad)  # type: ignore[arg-type]
    row = raw_row(tmp_path, task_id)
    assert row["status"] == "open"  # refused before any board mutation
    assert row["metadata"] == {}


async def test_close_task_keeps_unknown_status_absent(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    omitted = await adapter.close_task(task_id, "legacy close")
    assert omitted["outcome_status"] is None
    assert omitted["outcome_history"] == []
    assert omitted["outcome_status_history"] == []
    assert "theater_outcome_status" not in raw_row(tmp_path, task_id)["metadata"]
    await adapter.update_task(task_id, status="open")
    explicit = await adapter.close_task(task_id, "still unknown", None)
    assert explicit["outcome_status"] is None
    assert "theater_outcome_status" not in raw_row(tmp_path, task_id)["metadata"]


async def test_status_reads_only_while_closed_and_reopen_clears_it(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    closed = await adapter.close_task(task_id, "feature shipped", "succeeded")
    assert closed["outcome"] == "feature shipped"
    assert closed["outcome_status"] == "succeeded"
    metadata = raw_row(tmp_path, task_id)["metadata"]
    assert metadata["theater_outcome"] == "feature shipped"
    assert metadata["theater_outcome_status"] == "succeeded"

    reopened = await adapter.update_task(task_id, status="open")
    assert reopened["outcome"] is None
    assert reopened["outcome_status"] is None
    assert reopened["outcome_history"] == ["feature shipped"]
    assert reopened["outcome_status_history"] == ["succeeded"]
    metadata = raw_row(tmp_path, task_id)["metadata"]
    assert "theater_outcome" not in metadata
    assert "theater_outcome_status" not in metadata
    assert metadata["theater_generation"] == 1  # generation stays independent
    stored = json.loads(metadata["theater_outcome_history"])
    assert stored == [{"outcome": "feature shipped", "status": "succeeded"}]


async def test_mixed_history_is_one_canonical_list(adapter: BeadsService, tmp_path: Path) -> None:
    task_id = await make_task(adapter)
    await close_reopen(adapter, task_id, "typed failure", "failed")
    await close_reopen(adapter, task_id, "untyped close")
    await close_reopen(adapter, task_id, "typed cancel", "cancelled")
    await close_reopen(adapter, task_id, "typed success", "succeeded")

    detail = await adapter.get_task(task_id)
    assert detail["outcome_history"] == [
        "typed failure",
        "untyped close",
        "typed cancel",
        "typed success",
    ]
    assert detail["outcome_status_history"] == ["failed", None, "cancelled", "succeeded"]
    assert len(detail["outcome_history"]) == len(detail["outcome_status_history"])
    # one stored list; typed entries are objects, unknown stays a plain string
    stored = json.loads(raw_row(tmp_path, task_id)["metadata"]["theater_outcome_history"])
    assert stored == [
        {"outcome": "typed failure", "status": "failed"},
        "untyped close",
        {"outcome": "typed cancel", "status": "cancelled"},
        {"outcome": "typed success", "status": "succeeded"},
    ]


async def test_history_cap_and_generation_advance_together(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    for cycle in range(55):
        status = "failed" if cycle % 2 else None
        await close_reopen(adapter, task_id, f"cycle {cycle}", status)
    detail = await adapter.get_task(task_id)
    assert detail["outcome_history"] == [f"cycle {cycle}" for cycle in range(5, 55)]
    assert detail["outcome_status_history"] == [
        "failed" if cycle % 2 else None for cycle in range(5, 55)
    ]
    metadata = raw_row(tmp_path, task_id)["metadata"]
    assert metadata["theater_generation"] == 55
    assert len(json.loads(metadata["theater_outcome_history"])) == 50


async def test_legacy_history_reads_stable_and_grows_in_place(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    FakeBoard(tmp_path, "instance-one").command(
        "update",
        task_id,
        "--set-metadata",
        'theater_outcome_history=["legacy one", "legacy two"]',
        actor="author",
    )
    detail = await adapter.get_task(task_id)
    assert detail["outcome_history"] == ["legacy one", "legacy two"]
    assert detail["outcome_status_history"] == [None, None]

    await close_reopen(adapter, task_id, "typed now", "failed")
    detail = await adapter.get_task(task_id)
    assert detail["outcome_history"] == ["legacy one", "legacy two", "typed now"]
    assert detail["outcome_status_history"] == [None, None, "failed"]
    stored = json.loads(raw_row(tmp_path, task_id)["metadata"]["theater_outcome_history"])
    assert stored == ["legacy one", "legacy two", {"outcome": "typed now", "status": "failed"}]


async def test_malformed_history_and_status_stay_unknown(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    for value in ("not json ]", {"outcome": "not a list"}, 5):
        edit_row(
            tmp_path,
            task_id,
            lambda row, v=value: row["metadata"].update({"theater_outcome_history": v}),
        )
        detail = await adapter.get_task(task_id)
        assert detail["outcome_history"] == []
        assert detail["outcome_status_history"] == []

    # unreadable items coerce to text; unreadable statuses stay unknown
    edit_row(
        tmp_path,
        task_id,
        lambda row: row["metadata"].update(
            {
                "theater_outcome_history": json.dumps(
                    [5, None, {"outcome": "t", "status": "done"}, {"outcome": "u", "status": 5}]
                )
            }
        ),
    )
    detail = await adapter.get_task(task_id)
    assert detail["outcome_history"] == ["5", "None", "t", "u"]
    assert detail["outcome_status_history"] == [None, None, None, None]

    # a stored status we never wrote reads as unknown, never success
    stray = await make_task(adapter, "Stray status")
    edit_row(
        tmp_path,
        stray,
        lambda row: row["metadata"].update({"theater_outcome_status": "banana"}),
    )
    await adapter.close_task(stray, "closed over a stray status")
    assert (await adapter.get_task(stray))["outcome_status"] is None
    await adapter.update_task(stray, status="open")
    detail = await adapter.get_task(stray)
    assert detail["outcome_status_history"] == [None]  # retained as unknown, not success
    assert "theater_outcome_status" not in raw_row(tmp_path, stray)["metadata"]


async def test_failed_prerequisites_cover_deps_and_children(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter, "Target")
    dep_failed = await make_task(adapter, "Dep failed")
    dep_cancelled = await make_task(adapter, "Dep cancelled")
    dep_unknown = await make_task(adapter, "Dep unknown")
    dep_succeeded = await make_task(adapter, "Dep succeeded")
    child_failed = await make_task(adapter, "Child failed")
    child_succeeded = await make_task(adapter, "Child succeeded")
    child_open = await make_task(adapter, "Child open")
    for dep in (dep_failed, dep_cancelled, dep_unknown, dep_succeeded):
        await adapter.set_dependency(task_id, dep, True)
    for child in (child_failed, child_succeeded, child_open):
        await adapter.update_task(child, parent_id=task_id)
    await adapter.close_task(dep_failed, "broken", "failed")
    await adapter.close_task(dep_cancelled, "abandoned", "cancelled")
    await adapter.close_task(dep_unknown, "legacy close")
    await adapter.close_task(dep_succeeded, "green", "succeeded")
    await adapter.close_task(child_failed, "broken too", "failed")
    await adapter.close_task(child_succeeded, "green too", "succeeded")

    expected = sorted(
        [
            {"id": dep_failed, "outcome_status": "failed"},
            {"id": dep_cancelled, "outcome_status": "cancelled"},
            {"id": child_failed, "outcome_status": "failed"},
        ],
        key=lambda item: item["id"],
    )
    detail = await adapter.get_task(task_id)
    assert detail["failed_prerequisites"] == expected
    assert detail["truncated"] is False
    assert detail["blockers"] == sorted([child_open])  # native blockers untouched
    progress = await adapter.get_task(task_id, view="progress")
    assert progress["failed_prerequisites"] == expected
    assert progress["truncated"] is False

    # a task that is both dep and child appears once
    both = await make_task(adapter, "Both")
    await adapter.set_dependency(task_id, both, True)
    await adapter.update_task(both, parent_id=task_id)
    await adapter.close_task(both, "twice over", "failed")
    detail = await adapter.get_task(task_id)
    assert detail["failed_prerequisites"] == [*expected, {"id": both, "outcome_status": "failed"}]


async def test_failed_prerequisites_truncate_explicitly(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter, "Wide")
    failed_ids = []
    for index in range(205):
        child = await make_task(adapter, f"child-{index}")
        await adapter.update_task(child, parent_id=task_id)
        await adapter.close_task(child, f"failed {index}", "failed")
        failed_ids.append(child)
    detail = await adapter.get_task(task_id)
    expected = [{"id": task, "outcome_status": "failed"} for task in sorted(failed_ids)[:200]]
    assert detail["failed_prerequisites"] == expected
    assert detail["truncated"] is True
    progress = await adapter.get_task(task_id, view="progress")
    assert progress["failed_prerequisites"] == expected
    assert progress["truncated"] is True


async def test_summaries_carry_own_status_without_graph_scans(
    adapter: BeadsService, tmp_path: Path
) -> None:
    failed = await make_task(adapter, "Failed")
    unknown = await make_task(adapter, "Unknown")
    open_task = await make_task(adapter, "Open")
    await adapter.close_task(failed, "nope", "failed")
    await adapter.close_task(unknown, "legacy")
    listing = await adapter.list_tasks()
    rows = {row["id"]: row for row in listing["tasks"]}
    assert rows[failed]["outcome_status"] == "failed"
    assert rows[unknown]["outcome_status"] is None
    assert rows[open_task]["outcome_status"] is None
    assert all("failed_prerequisites" not in row for row in listing["tasks"])


async def test_assignee_resolves_names_case_insensitively(
    adapter: BeadsService, tmp_path: Path
) -> None:
    for spelling in ("Author", "AUTHOR", "aUtHoR"):
        created = await adapter.create_task(f"By {spelling}", assignee=spelling)
        assert created["assignee"] == "author"
    by_id = await adapter.create_task("By exact ID", assignee="peer")
    assert by_id["assignee"] == "peer"
    updated = await adapter.update_task(by_id["id"], assignee="PEER")
    assert updated["assignee"] == "peer"

    # an exact ID stays unambiguous even when a member is named like another ID
    FakeClient.members = [
        {"participant_id": "65059c691445", "name": "Author"},
        {"participant_id": "peer", "name": "65059c691445"},
    ]
    by_exact = await adapter.create_task("ID precedence", assignee="65059c691445")
    assert by_exact["assignee"] == "65059c691445"


async def test_casefolded_duplicate_names_fail_closed(
    adapter: BeadsService, tmp_path: Path
) -> None:
    FakeClient.members = [
        {"participant_id": "m1", "name": "Florindo"},
        {"participant_id": "m2", "name": "florindo"},
    ]
    task_id = await make_task(adapter, "Ambiguous")
    # even the exact spelling of one member refuses: the alias picks no member
    for spelling in ("Florindo", "florindo", "FLORINDO"):
        with pytest.raises(BeadsError, match="shared by several live members"):
            await adapter.create_task("Never created", assignee=spelling)
        with pytest.raises(BeadsError, match="shared by several live members"):
            await adapter.update_task(task_id, assignee=spelling)
    # the exact member IDs keep working through the same ambiguity
    assert (await adapter.update_task(task_id, assignee="m2"))["assignee"] == "m2"
    assert raw_row(tmp_path, task_id)["assignee"] == "m2"


async def test_assignee_refusal_lists_bounded_member_suggestions(
    adapter: BeadsService, tmp_path: Path
) -> None:
    FakeClient.members = [
        {"participant_id": "author", "name": "Author"},
        {"participant_id": "peer", "name": "Peer"},
    ]
    with pytest.raises(BeadsError, match="must be a live member ID or name") as refused:
        await adapter.create_task("Ghost", assignee="Ghost")
    assert "known members: Author (author), Peer (peer)" in str(refused.value)

    FakeClient.members = [
        {"participant_id": f"m{i:02}", "name": f"Member{i:02}"} for i in range(25)
    ] + [{"participant_id": "noname", "name": ""}]  # nameless members are not suggested
    with pytest.raises(BeadsError) as many:
        await adapter.create_task("Ghost", assignee="Ghost")
    text = str(many.value)
    assert "Member00 (m00)" in text
    assert "Member19 (m19)" in text
    assert "Member20 (m20)" not in text
    assert "and 5 more" in text
    assert "noname" not in text
