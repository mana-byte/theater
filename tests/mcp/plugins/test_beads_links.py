"""Job links, generations, guards and the progress view on the fake Beads board.

The service runs against ``FakeClient`` and ``FakeBoard``; every assertion about
facts reads ``FakeClient.facts_calls``, so these tests pin the exact enrichment
budget of each surface: detail never fetches, progress batches, links fetch
only what they authorize.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.support.beads_fakes import FakeBoard, FakeClient, fact, instance
from theater.mcp_plugins.builtin.beads import service
from theater.mcp_plugins.builtin.beads.backend import BeadsError
from theater.mcp_plugins.builtin.beads.service import BeadsService


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BeadsService:
    monkeypatch.setattr(service, "TheaterPluginClient", FakeClient)
    monkeypatch.setattr(service, "Board", FakeBoard)
    FakeClient.reset()
    return BeadsService(tmp_path)


async def make_task(adapter: BeadsService, title: str = "Linked task") -> str:
    return (await adapter.create_task(title))["id"]


def raw_row(tmp_path: Path, task_id: str) -> dict[str, Any]:
    return FakeBoard(tmp_path, "instance-one").rows()[task_id]


def edit_row(tmp_path: Path, task_id: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    board = FakeBoard(tmp_path, "instance-one")
    mutate(board.rows()[task_id])
    board._save()


def seed_links(
    tmp_path: Path,
    task_id: str,
    count: int,
    *,
    role: str = "work",
    generation: int = 0,
    base: float = 1000.0,
) -> list[dict[str, Any]]:
    records = [
        {
            "version": 1,
            "handle": f"seeded-{i:03}",
            "role": role,
            "review_of": None,
            "generation": generation,
            "created_at": base + i,
        }
        for i in range(count)
    ]
    FakeBoard(tmp_path, "instance-one").command(
        "update", task_id, "--metadata", json.dumps({"theater_links": records}), actor="author"
    )
    return records


def variant(base: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload = dict(base)
    payload.update(overrides)
    return payload


async def review_progress(
    adapter: BeadsService, work_fact: dict[str, Any], review_fact: dict[str, Any]
) -> dict[str, Any]:
    task_id = (await adapter.create_task("Reviewed"))["id"]
    FakeClient.facts = {"work-1": work_fact, "review-1": review_fact}
    await adapter.link_task_job(task_id, "work-1", "work")
    await adapter.link_task_job(task_id, "review-1", "review", "work-1")
    return await adapter.get_task(task_id, view="progress")


async def test_detail_enriches_nothing_and_view_is_validated(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    FakeClient.facts = {"work-a": fact("work-a")}
    await adapter.link_task_job(task_id, "work-a", "work")
    calls = FakeClient.facts_calls

    detail = await adapter.get_task(task_id)
    assert "job_links" not in detail
    assert FakeClient.facts_calls == calls

    progress = await adapter.get_task(task_id, view="progress")
    assert progress["job_links"][0]["handle"] == "work-a"
    assert FakeClient.facts_calls == calls + 1
    with pytest.raises(BeadsError, match="view must be detail or progress"):
        await adapter.get_task(task_id, view="brief")  # type: ignore[arg-type]


async def test_link_parameter_validation(adapter: BeadsService, tmp_path: Path) -> None:
    task_id = await make_task(adapter)
    for bad in (None, "   ", "x" * 257):
        with pytest.raises(BeadsError, match="exact job handle"):
            await adapter.link_task_job(task_id, bad, "work")  # type: ignore[arg-type]
    with pytest.raises(BeadsError, match="role must be work or review"):
        await adapter.link_task_job(task_id, "work-a", "audit")  # type: ignore[arg-type]
    with pytest.raises(BeadsError, match="work link cannot name review_of"):
        await adapter.link_task_job(task_id, "work-a", "work", "work-b")
    with pytest.raises(BeadsError, match="review_of must be an exact job handle"):
        await adapter.link_task_job(task_id, "work-a", "review")
    assert "theater_links" not in raw_row(tmp_path, task_id)["metadata"]

    FakeClient.facts = {"h" * 256: fact("h" * 256)}
    assert (await adapter.link_task_job(task_id, "h" * 256, "work"))["links_total"] == 1


async def test_link_requires_dispatched_play_proofs(adapter: BeadsService, tmp_path: Path) -> None:
    task_id = await make_task(adapter)
    refusals: list[tuple[dict[str, Any], str]] = [
        (fact("work-b", available=False), "is not available"),
        (
            fact(
                "work-b",
                play={"instance_id": "instance-two", "caller_seated_at_creation": True},
            ),
            "no play proof",
        ),
        (
            fact(
                "work-b", play={"instance_id": "instance-one", "caller_seated_at_creation": False}
            ),
            "seated in this play",
        ),
        (fact("work-b", created_at=None), "no usable creation time"),
    ]
    for payload, message in refusals:
        FakeClient.facts = {"work-b": payload}
        with pytest.raises(BeadsError, match=message):
            await adapter.link_task_job(task_id, "work-b", "work")
    FakeClient.facts = {}
    with pytest.raises(BeadsError, match="is not available"):
        await adapter.link_task_job(task_id, "ghost-job", "work")
    assert "theater_links" not in raw_row(tmp_path, task_id)["metadata"]

    FakeClient.permissions = ["read"]
    with pytest.raises(BeadsError, match="no Beads write grant"):
        await adapter.link_task_job(task_id, "work-b", "work")
    FakeClient.permissions = ["read", "write"]

    token = instance.set("instance-two")
    try:
        with pytest.raises(BeadsError, match="must identify an existing task in this play"):
            await adapter.link_task_job(task_id, "work-b", "work")
    finally:
        instance.reset(token)


async def test_duplicate_replays_cost_nothing_and_conflicts_refuse(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    FakeClient.facts = {"work-a": fact("work-a")}
    first = await adapter.link_task_job(task_id, "work-a", "work")
    assert first["duplicate"] is False
    assert first["generation"] == 0
    assert first["links_total"] == 1

    FakeClient.facts = {}
    FakeClient.facts_calls = 0
    replay = await adapter.link_task_job(task_id, "work-a", "work")
    assert replay["duplicate"] is True
    assert replay["links_total"] == 1
    assert FakeClient.facts_calls == 0
    with pytest.raises(BeadsError, match="changing a stored link"):
        await adapter.link_task_job(task_id, "work-a", "review", "work-a")
    assert FakeClient.facts_calls == 0

    FakeClient.facts = {"review-b": fact("review-b"), "work-c": fact("work-c")}
    await adapter.link_task_job(task_id, "work-c", "work")
    review = await adapter.link_task_job(task_id, "review-b", "review", "work-a")
    assert review["links_total"] == 3
    FakeClient.facts = {}
    FakeClient.facts_calls = 0
    again = await adapter.link_task_job(task_id, "review-b", "review", "work-a")
    assert again["duplicate"] is True
    with pytest.raises(BeadsError, match="changing a stored link"):
        await adapter.link_task_job(task_id, "review-b", "review", "work-c")
    assert FakeClient.facts_calls == 0


async def test_review_of_must_name_a_work_link_on_the_same_task(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    FakeClient.facts = {
        "work-a": fact("work-a"),
        "work-z": fact("work-z"),
        "review-b": fact("review-b"),
        "review-c": fact("review-c"),
    }
    with pytest.raises(BeadsError, match="review_of must name a work job"):
        await adapter.link_task_job(task_id, "review-b", "review", "work-a")
    await adapter.link_task_job(task_id, "work-a", "work")
    await adapter.link_task_job(task_id, "review-b", "review", "work-a")
    with pytest.raises(BeadsError, match="review_of must name a work job"):
        await adapter.link_task_job(task_id, "review-c", "review", "review-b")
    other_id = await make_task(adapter, "Other task")
    await adapter.link_task_job(other_id, "work-z", "work")
    with pytest.raises(BeadsError, match="review_of must name a work job"):
        await adapter.link_task_job(task_id, "review-c", "review", "work-z")
    assert len(raw_row(tmp_path, task_id)["metadata"]["theater_links"]) == 2


async def test_link_cap_and_duplicate_at_cap(adapter: BeadsService, tmp_path: Path) -> None:
    task_id = await make_task(adapter)
    seed_links(tmp_path, task_id, count=100)
    FakeClient.facts = {"work-new": fact("work-new")}
    with pytest.raises(BeadsError, match="at most 100 job links"):
        await adapter.link_task_job(task_id, "work-new", "work")

    FakeClient.facts = {}
    FakeClient.facts_calls = 0
    replay = await adapter.link_task_job(task_id, "seeded-000", "work")
    assert replay["duplicate"] is True
    assert replay["links_total"] == 100
    assert FakeClient.facts_calls == 0

    progress = await adapter.get_task(task_id, view="progress")
    assert len(progress["job_links"]) == 100
    assert progress["current_work"]["handle"] == "seeded-099"
    assert progress["current_work"]["available"] is False


async def test_generation_advances_past_fifty_reopens(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    for cycle in range(55):
        await adapter.close_task(task_id, f"cycle {cycle}")
        await adapter.update_task(task_id, status="open")
    metadata = raw_row(tmp_path, task_id)["metadata"]
    assert metadata["theater_generation"] == 55
    assert len(json.loads(metadata["theater_outcome_history"])) == 50

    FakeClient.facts = {"work-late": fact("work-late")}
    linked = await adapter.link_task_job(task_id, "work-late", "work")
    assert linked["generation"] == 55
    progress = await adapter.get_task(task_id, view="progress")
    assert progress["generation"] == 55
    assert progress["current_work"]["handle"] == "work-late"
    assert progress["boundary_fallback"] is False


async def test_native_close_boundary_and_stale_refusal(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    FakeBoard(tmp_path, "instance-one").command(
        "update", task_id, "--status", "closed", actor="author"
    )
    native = datetime.fromisoformat(raw_row(tmp_path, task_id)["closed_at"])
    reopened = await adapter.update_task(task_id, status="open")
    assert reopened["outcome"] is None
    metadata = raw_row(tmp_path, task_id)["metadata"]
    assert metadata["theater_generation"] == 1
    assert abs(metadata["theater_boundary"] - native.timestamp()) < 1.0
    assert "theater_boundary_fallback" not in metadata

    FakeClient.facts = {
        "work-old": fact("work-old", created_at=native.timestamp() - 60),
        "work-new": fact("work-new"),
    }
    with pytest.raises(BeadsError, match="stale generation"):
        await adapter.link_task_job(task_id, "work-old", "work")
    assert (await adapter.link_task_job(task_id, "work-new", "work"))["generation"] == 1


async def test_unknown_close_time_gets_a_fallback_boundary(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    FakeBoard(tmp_path, "instance-one").command(
        "update", task_id, "--status", "closed", actor="author"
    )
    edit_row(tmp_path, task_id, lambda row: row.pop("closed_at"))
    before = time.time()
    await adapter.update_task(task_id, status="open")
    metadata = raw_row(tmp_path, task_id)["metadata"]
    assert metadata["theater_boundary_fallback"] == 1
    assert metadata["theater_boundary"] >= before - 1.0

    FakeClient.facts = {"work-new": fact("work-new")}
    assert (await adapter.link_task_job(task_id, "work-new", "work"))["generation"] == 1
    progress = await adapter.get_task(task_id, view="progress")
    assert progress["boundary_fallback"] is True


async def test_closed_guard_and_repair_reopen_link(adapter: BeadsService, tmp_path: Path) -> None:
    task_id = await make_task(adapter)
    await adapter.close_task(task_id, "first cycle done")
    FakeClient.facts = {"repair-1": fact("repair-1")}
    with pytest.raises(BeadsError, match="after this task closed"):
        await adapter.link_task_job(task_id, "repair-1", "work")

    def strip_close(row: dict[str, Any]) -> None:
        row.pop("closed_at", None)
        row["metadata"].pop("theater_closed_at", None)

    edit_row(tmp_path, task_id, strip_close)
    with pytest.raises(BeadsError, match="close time is unknown"):
        await adapter.link_task_job(task_id, "repair-1", "work")

    await adapter.update_task(task_id, status="open")
    FakeClient.facts = {"repair-1": fact("repair-1")}
    assert (await adapter.link_task_job(task_id, "repair-1", "work"))["generation"] == 1


async def test_selection_prefers_newest_and_reports_ties(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    records = [
        {
            "version": 1,
            "handle": "work-old",
            "role": "work",
            "review_of": None,
            "generation": 0,
            "created_at": 100.0,
        },
        {
            "version": 1,
            "handle": "work-new",
            "role": "work",
            "review_of": None,
            "generation": 0,
            "created_at": 200.0,
        },
        {
            "version": 1,
            "handle": "work-tied-b",
            "role": "work",
            "review_of": None,
            "generation": 0,
            "created_at": 300.0,
        },
        {
            "version": 1,
            "handle": "work-tied-a",
            "role": "work",
            "review_of": None,
            "generation": 0,
            "created_at": 300.0,
        },
        {
            "version": 1,
            "handle": "work-other-gen",
            "role": "work",
            "review_of": None,
            "generation": 7,
            "created_at": 900.0,
        },
    ]
    FakeBoard(tmp_path, "instance-one").command(
        "update", task_id, "--metadata", json.dumps({"theater_links": records}), actor="author"
    )
    progress = await adapter.get_task(task_id, view="progress")
    assert progress["current_work"] == {
        "ambiguous": True,
        "created_at": 300.0,
        "handles": ["work-tied-a", "work-tied-b"],
    }
    assert [entry["handle"] for entry in progress["job_links"]] == [
        "work-old",
        "work-new",
        "work-tied-b",
        "work-tied-a",
        "work-other-gen",
    ]


async def test_current_work_is_the_newest_even_when_cancelled(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    records = [
        {
            "version": 1,
            "handle": "work-done",
            "role": "work",
            "review_of": None,
            "generation": 0,
            "created_at": 100.0,
        },
        {
            "version": 1,
            "handle": "work-cancelled",
            "role": "work",
            "review_of": None,
            "generation": 0,
            "created_at": 200.0,
        },
    ]
    FakeBoard(tmp_path, "instance-one").command(
        "update", task_id, "--metadata", json.dumps({"theater_links": records}), actor="author"
    )
    FakeClient.facts = {
        "work-done": fact("work-done"),
        "work-cancelled": fact("work-cancelled", state="cancelled"),
    }
    progress = await adapter.get_task(task_id, view="progress")
    assert progress["current_work"]["handle"] == "work-cancelled"
    assert progress["current_work"]["state"] == "cancelled"


async def test_review_evidence_branches(adapter: BeadsService) -> None:
    work_fact = fact("work-1", finished_at=1000.0)
    review_fact = fact(
        "review-1",
        created_at=2000.0,
        contract_preset="review",
        review_verdict="approve",
        target_id="target-two",
    )
    handoff = {"source_handle": "work-1", "task_kind": "independent_review"}

    matched_handoff = await review_progress(
        adapter, work_fact, variant(review_fact, handoff=handoff)
    )
    assert matched_handoff["latest_review"]["artifact_relation"] == "matched"
    assert matched_handoff["latest_review"]["source_basis"] == "handoff"

    handoff_mismatch = await review_progress(
        adapter,
        work_fact,
        variant(review_fact, handoff={"source_handle": "work-other", "task_kind": "work"}),
    )
    assert handoff_mismatch["latest_review"]["artifact_relation"] == "unverified"

    declared = await review_progress(adapter, work_fact, review_fact)
    assert declared["latest_review"]["artifact_relation"] == "matched"
    assert declared["latest_review"]["source_basis"] == "declared"

    too_early = await review_progress(adapter, work_fact, variant(review_fact, created_at=999.0))
    assert too_early["latest_review"]["artifact_relation"] == "unverified"

    self_review = await review_progress(
        adapter, work_fact, variant(review_fact, target_id="target-one")
    )
    assert self_review["latest_review"]["artifact_relation"] == "unverified"

    dirty_work = await review_progress(
        adapter,
        variant(
            work_fact,
            git={"pin_state": "pinned", "commit_sha": "abc123", "dirty_entries": 2, "clean": False},
        ),
        review_fact,
    )
    assert dirty_work["latest_review"]["artifact_relation"] == "unverified"

    other_commit = await review_progress(
        adapter,
        work_fact,
        variant(
            review_fact,
            git={"pin_state": "pinned", "commit_sha": "def789", "dirty_entries": 0, "clean": True},
        ),
    )
    assert other_commit["latest_review"]["artifact_relation"] == "unverified"

    rejected = await review_progress(
        adapter, work_fact, variant(review_fact, review_verdict="reject")
    )
    assert rejected["latest_review"]["artifact_relation"] == "unverified"

    lookalike = await review_progress(
        adapter, work_fact, variant(review_fact, contract_preset="reviewish")
    )
    assert lookalike["latest_review"]["artifact_relation"] == "unverified"

    task_id = (await adapter.create_task("Work fact gone"))["id"]
    FakeClient.facts = {"work-1": work_fact, "review-1": variant(review_fact, handoff=handoff)}
    await adapter.link_task_job(task_id, "work-1", "work")
    await adapter.link_task_job(task_id, "review-1", "review", "work-1")
    FakeClient.facts.pop("work-1")
    progress = await adapter.get_task(task_id, view="progress")
    assert progress["latest_review"]["source_basis"] == "handoff"
    assert progress["latest_review"]["artifact_relation"] == "unavailable"

    task_id = (await adapter.create_task("Review fact gone"))["id"]
    FakeClient.facts = {"work-1": work_fact, "review-1": review_fact}
    await adapter.link_task_job(task_id, "work-1", "work")
    await adapter.link_task_job(task_id, "review-1", "review", "work-1")
    FakeClient.facts.pop("review-1")
    progress = await adapter.get_task(task_id, view="progress")
    assert progress["latest_review"]["source_basis"] is None
    assert progress["latest_review"]["artifact_relation"] == "unavailable"


async def test_progress_batches_facts_and_bounds_blockers(
    adapter: BeadsService, tmp_path: Path
) -> None:
    empty_id = await make_task(adapter, "Empty")
    progress = await adapter.get_task(empty_id, view="progress")
    assert progress["job_links"] == []
    assert FakeClient.facts_calls == 0

    fifty_id = await make_task(adapter, "Fifty")
    seed_links(tmp_path, fifty_id, count=50)
    await adapter.get_task(fifty_id, view="progress")
    assert FakeClient.facts_calls == 1

    hundred_id = await make_task(adapter, "Hundred")
    seed_links(tmp_path, hundred_id, count=100)
    await adapter.get_task(hundred_id, view="progress")
    assert FakeClient.facts_calls == 3

    blocked_id = await make_task(adapter, "Blocked")
    board = FakeBoard(tmp_path, "instance-one")
    for index in range(205):
        board.command("create", f"--title=child-{index}", "--parent", blocked_id, actor="author")
    progress = await adapter.get_task(blocked_id, view="progress")
    assert len(progress["blockers"]) == 200
    assert progress["truncated"] is True


async def test_concurrent_links_settle_on_one_record(adapter: BeadsService, tmp_path: Path) -> None:
    task_id = await make_task(adapter)
    FakeClient.facts = {"work-a": fact("work-a")}
    first, second = await asyncio.gather(
        adapter.link_task_job(task_id, "work-a", "work"),
        adapter.link_task_job(task_id, "work-a", "work"),
    )
    assert sorted((first["duplicate"], second["duplicate"])) == [False, True]
    assert first["links_total"] == 1
    assert second["links_total"] == 1
    assert FakeClient.facts_calls == 1
    stored = [
        record
        for record in raw_row(tmp_path, task_id)["metadata"]["theater_links"]
        if record["handle"] == "work-a"
    ]
    assert len(stored) == 1

    results = await asyncio.gather(
        adapter.link_task_job(task_id, "work-a", "review", "work-a"),
        adapter.link_task_job(task_id, "work-a", "work"),
        return_exceptions=True,
    )
    assert sum(isinstance(value, BeadsError) for value in results) == 1
    assert sum(isinstance(value, dict) for value in results) == 1
    assert len(raw_row(tmp_path, task_id)["metadata"]["theater_links"]) == 1


async def test_links_preserve_foreign_metadata(adapter: BeadsService, tmp_path: Path) -> None:
    task_id = await make_task(adapter)
    FakeBoard(tmp_path, "instance-one").command(
        "update",
        task_id,
        "--metadata",
        json.dumps({"owner_note": "keep me", "tags": ["alpha", "beta"]}),
        actor="author",
    )
    FakeClient.facts = {"work-a": fact("work-a")}
    await adapter.link_task_job(task_id, "work-a", "work")
    await adapter.close_task(task_id, "done; work linked")
    await adapter.update_task(task_id, status="open")

    metadata = raw_row(tmp_path, task_id)["metadata"]
    assert metadata["owner_note"] == "keep me"
    assert metadata["tags"] == ["alpha", "beta"]
    assert json.loads(metadata["theater_outcome_history"]) == ["done; work linked"]
    assert "theater_outcome" not in metadata
    assert "theater_closed_at" not in metadata
    assert len(metadata["theater_links"]) == 1
    assert metadata["theater_generation"] == 1
    assert isinstance(metadata["theater_boundary"], float)


def stored_link(handle: str = "work-a", **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "version": 1,
        "handle": handle,
        "role": "work",
        "review_of": None,
        "generation": 0,
        "created_at": 1000.0,
    }
    record.update(overrides)
    return record


def seed_raw_links(tmp_path: Path, task_id: str, records: object) -> None:
    FakeBoard(tmp_path, "instance-one").command(
        "update", task_id, "--metadata", json.dumps({"theater_links": records}), actor="author"
    )


async def test_malformed_handoff_never_matches(adapter: BeadsService) -> None:
    work_fact = fact("work-1", finished_at=1000.0)
    review_fact = fact(
        "review-1",
        created_at=2000.0,
        contract_preset="review",
        review_verdict="approve",
        target_id="target-two",
    )

    # A present but unusable handoff never takes the declared-timing branch.
    for handoff in ("malformed", 5, [1, 2]):
        progress = await review_progress(adapter, work_fact, variant(review_fact, handoff=handoff))
        assert progress["latest_review"]["artifact_relation"] == "unverified"
        assert progress["latest_review"]["source_basis"] is None
        assert progress["job_links"][1]["source_basis"] is None

    # A gate object without a source never matches either, and says which basis ran.
    progress = await review_progress(
        adapter, work_fact, variant(review_fact, handoff={"task_kind": "independent_review"})
    )
    assert progress["latest_review"]["artifact_relation"] == "unverified"
    assert progress["latest_review"]["source_basis"] == "handoff"


async def test_nonfinite_times_are_unusable(adapter: BeadsService) -> None:
    work_fact = fact("work-1", finished_at=1000.0)
    review_fact = fact(
        "review-1",
        created_at=2000.0,
        contract_preset="review",
        review_verdict="approve",
        target_id="target-two",
    )

    # Linking needs a usable creation time; drift the fact afterwards to hit the
    # relation, where a nonfinite time must not pass the declared comparison.
    for created_at in (math.nan, math.inf, -math.inf, "NaN", "Infinity"):
        task_id = (await adapter.create_task("Reviewed"))["id"]
        FakeClient.facts = {"work-1": work_fact, "review-1": review_fact}
        await adapter.link_task_job(task_id, "work-1", "work")
        await adapter.link_task_job(task_id, "review-1", "review", "work-1")
        FakeClient.facts["review-1"] = variant(review_fact, created_at=created_at)
        progress = await adapter.get_task(task_id, view="progress")
        assert progress["latest_review"]["artifact_relation"] == "unverified"

    for finished_at in (math.nan, math.inf, "NaN"):
        task_id = (await adapter.create_task("Reviewed"))["id"]
        FakeClient.facts = {"work-1": work_fact, "review-1": review_fact}
        await adapter.link_task_job(task_id, "work-1", "work")
        await adapter.link_task_job(task_id, "review-1", "review", "work-1")
        FakeClient.facts["work-1"] = variant(work_fact, finished_at=finished_at)
        progress = await adapter.get_task(task_id, view="progress")
        assert progress["latest_review"]["artifact_relation"] == "unverified"

    task_id = await make_task(adapter)
    for created_at in (math.nan, math.inf, "NaN", "Infinity"):
        FakeClient.facts = {"work-a": fact("work-a", created_at=created_at)}
        with pytest.raises(BeadsError, match="no usable creation time"):
            await adapter.link_task_job(task_id, "work-a", "work")


async def test_corrupt_stored_links_fail_closed(adapter: BeadsService, tmp_path: Path) -> None:
    bad_shapes = [
        stored_link(version=999),  # an unknown version is not a verified duplicate
        stored_link(role="audit"),
        stored_link(review_of="work-b"),  # work record naming review_of
        stored_link(created_at=math.nan),  # a malformed time drops no newest work
        stored_link(created_at=None),
        stored_link(generation=-1),
        stored_link(handle="x" * 257),
        "not an object",
    ]
    for record in bad_shapes:
        task_id = await make_task(adapter)
        seed_raw_links(tmp_path, task_id, [record])
        calls = FakeClient.facts_calls
        with pytest.raises(BeadsError, match="unsupported or corrupt"):
            await adapter.get_task(task_id, view="progress")
        FakeClient.facts = {"work-b": fact("work-b")}
        with pytest.raises(BeadsError, match="unsupported or corrupt"):
            await adapter.link_task_job(task_id, "work-b", "work")
        assert FakeClient.facts_calls == calls  # refused before any facts read
        # the corrupt record is never rewritten or dropped
        assert len(raw_row(tmp_path, task_id)["metadata"]["theater_links"]) == 1

    # an over-cap list refuses instead of evicting or spilling fact batches
    task_id = await make_task(adapter)
    seed_raw_links(tmp_path, task_id, [stored_link(f"h-{i:03}") for i in range(101)])
    with pytest.raises(BeadsError, match="over the cap of 100"):
        await adapter.get_task(task_id, view="progress")
    with pytest.raises(BeadsError, match="over the cap of 100"):
        await adapter.link_task_job(task_id, "work-b", "work")

    for payload in ({"handle": "work-a"}, "not json ]"):
        task_id = await make_task(adapter)

        def stash(row: dict[str, Any], value: object = payload) -> None:
            row["metadata"]["theater_links"] = value

        edit_row(tmp_path, task_id, stash)
        with pytest.raises(BeadsError, match="unsupported or corrupt"):
            await adapter.get_task(task_id, view="progress")


async def test_corrupt_generation_metadata_blocks_reads_and_reopen(
    adapter: BeadsService, tmp_path: Path
) -> None:
    task_id = await make_task(adapter)
    FakeClient.facts = {"work-a": fact("work-a")}
    await adapter.link_task_job(task_id, "work-a", "work")
    await adapter.close_task(task_id, "done; corrupting the bookkeeping")

    cases = (
        # key, value, progress refuses, reopen refuses
        ("theater_generation", "soon", True, True),  # else reopen revives generation 0
        ("theater_boundary", "NaN", True, False),  # reopen rewrites the boundary wholesale
        ("theater_boundary_fallback", "yes", True, False),
        ("theater_closed_at", "banana", False, True),  # progress never reads the close time
    )
    for key, value, blocks_progress, blocks_reopen in cases:

        def stash(row: dict[str, Any], k: str = key, v: object = value) -> None:
            row["metadata"][k] = v

        edit_row(tmp_path, task_id, stash)
        if blocks_progress:
            with pytest.raises(BeadsError, match="unsupported or corrupt"):
                await adapter.get_task(task_id, view="progress")
        FakeClient.facts = {"work-b": fact("work-b")}
        with pytest.raises(BeadsError, match="unsupported or corrupt"):
            await adapter.link_task_job(task_id, "work-b", "work")
        if blocks_reopen:
            with pytest.raises(BeadsError, match="unsupported or corrupt"):
                await adapter.update_task(task_id, status="open")

        def restore(row: dict[str, Any], k: str = key) -> None:
            row["metadata"].pop(k, None)

        edit_row(tmp_path, task_id, restore)

    # the untouched task still reopens, advances and links
    await adapter.update_task(task_id, status="open")
    FakeClient.facts = {"work-c": fact("work-c")}
    result = await adapter.link_task_job(task_id, "work-c", "work")
    assert result["generation"] == 1 and result["duplicate"] is False
    assert raw_row(tmp_path, task_id)["metadata"]["theater_links"][0]["handle"] == "work-a"
