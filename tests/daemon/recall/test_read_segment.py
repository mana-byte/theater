"""Reading a job segment: its metadata and a bounded, trust-checked transcript page."""

from __future__ import annotations

import json
import threading

import pytest

from tests.support.recall_segments import add_touch, append_records, make_job, vibe_session
from tests.support.shipped import VibeHarness
from theater.daemon.observer import Observer
from theater.daemon.recall_read import read_segment
from theater.provenance import TranscriptProvenance


async def test_job_segment_returns_metadata_and_transcript(registry, tmp_path):
    """A job segment returns the job's database metadata and its transcript.

    Goes through ``open_source`` so an adapter whose output is a file
    (vibe) answers the same way one whose output is a database would.
    The transcript events are unclipped and filtered to readable kinds.
    """
    root, project, sid, transcript = vibe_session(tmp_path)

    # Swap the shipped vibe harness for one pointed at our temp root,
    # so the observer finds our test transcript.
    from theater import harness as harness_mod

    vibe = VibeHarness(root=root)
    harness_mod.HARNESSES["vibe"] = vibe

    append_records(
        transcript,
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "it is done"},
    )

    make_job(
        registry.store,
        registry,
        handle="vibe-abc",
        target_cwd=str(project),
        session_id=sid,
    )
    add_touch(
        registry.store,
        handle="vibe-abc",
        path="src.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = await read_segment(
        "vibe-abc", store=registry.store, registry=registry, cwd=str(tmp_path)
    )

    assert result["kind"] == "job"
    assert result["handle"] == "vibe-abc"
    assert result["task"] == "do the thing"
    assert result["result"] == "it is done"
    assert result["outcome"] == "done"
    assert result["harness"] == "vibe"
    assert result["session_id"] == sid
    assert len(result["paths"]) == 1
    assert result["paths"][0]["path"] == "src.py"
    assert result["paths"][0]["sha_before"] == "aaa"
    assert result["paths"][0]["sha_after"] == "bbb"
    # The transcript must be available and carry the events we wrote.
    assert result["transcript"]["available"] is True
    roles = [e["role"] for e in result["transcript"]["events"]]
    assert "user" in roles
    assert "assistant" in roles


async def test_recall_reads_only_a_bounded_history_page_off_loop(registry, tmp_path, monkeypatch):
    from theater import harness as harness_mod
    from theater.daemon import recall_history, recall_read
    from theater.harness.transcript.source import TranscriptSource

    root, project, sid, transcript = vibe_session(tmp_path)
    monkeypatch.setitem(harness_mod.HARNESSES, "vibe", VibeHarness(root=root))
    append_records(
        transcript,
        *({"role": "assistant", "content": f"{index}:" + "x" * 600} for index in range(1000)),
    )
    make_job(registry.store, registry, handle="bounded", target_cwd=str(project), session_id=sid)
    loop_thread = threading.get_ident()
    original_open = recall_history.open_participant_source
    original_budget = recall_read._apply_response_budget
    worker_stages = []

    def opened(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        worker_stages.append("history")
        return original_open(*args, **kwargs)

    def budgeted(brief):
        assert threading.get_ident() != loop_thread
        worker_stages.append("budget")
        original_budget(brief)

    def unbounded(*_args, **_kwargs):
        pytest.fail("recall must never scan the entire archive")

    monkeypatch.setattr(recall_history, "open_participant_source", opened)
    monkeypatch.setattr(recall_read, "_apply_response_budget", budgeted)
    monkeypatch.setattr(TranscriptSource, "_read_all", unbounded)
    result = await read_segment(
        "bounded", store=registry.store, registry=registry, cwd=str(project)
    )
    history = result["transcript"]
    assert history["available"] is True
    assert 0 < len(history["events"]) <= 200
    assert history["events"][-1]["text"].startswith("999:")
    assert history["has_older"] is True and history["truncated"] is True
    assert history["dropped_events"] is None
    assert "read_transcript" in history["truncation_note"]
    assert worker_stages == ["history", "budget"]


async def test_job_segment_refuses_a_contested_heuristic_transcript(registry, tmp_path):
    """Recall must not bypass read_transcript's Fulgenzio/Senterello guard."""
    root, project, sid, transcript = vibe_session(tmp_path)
    from theater import harness as harness_mod

    harness_mod.HARNESSES["vibe"] = VibeHarness(root=root)
    append_records(transcript, {"role": "assistant", "content": "belongs to the sibling"})
    target_id = make_job(
        registry.store,
        registry,
        handle="ambiguous-vibe",
        target_cwd=str(project),
        session_id=sid,
    )
    domain = str(root.resolve())
    target = registry.get(target_id)
    target.session_correlation = str(TranscriptProvenance.HEURISTIC)
    target.transcript_domain = domain
    registry.store.upsert_participant(target)
    sibling = registry.register(harness="vibe", pane=None, cwd=str(project))
    sibling.transcript_domain = domain
    registry.store.upsert_participant(sibling)

    result = await read_segment(
        "ambiguous-vibe",
        store=registry.store,
        registry=registry,
        cwd=str(tmp_path),
    )

    assert result["transcript"]["available"] is False
    assert result["transcript"]["error_code"] == "transcript_correlation_untrusted"


async def test_job_segment_reports_transcript_identity_lost(registry, tmp_path):
    root, project, sid, _transcript = vibe_session(tmp_path)
    from theater import harness as harness_mod

    harness_mod.HARNESSES["vibe"] = VibeHarness(root=root)
    target_id = make_job(
        registry.store,
        registry,
        handle="lost-vibe",
        target_cwd=str(project),
        session_id=sid,
        session_correlation=str(TranscriptProvenance.OPERATOR),
    )
    target = registry.get(target_id)
    target.transcript_location = str(tmp_path / "gone" / "messages.jsonl")
    target.transcript_domain = str(root.resolve())
    registry.store.upsert_participant(target)
    observer = Observer(registry, harnesses={})

    result = await read_segment(
        "lost-vibe",
        store=registry.store,
        registry=registry,
        cwd=str(tmp_path),
        observer=observer,
    )

    assert result["transcript"]["available"] is False
    assert result["transcript"]["error_code"] == "transcript_identity_lost"
    assert f"theater candidates {target_id}" in result["transcript"]["reason"]
    assert (
        f"theater bind {target_id} <candidate> --confirm-id {target_id}"
        in (result["transcript"]["reason"])
    )
    assert not observer.transcript_identity_lost(target_id)
    assert not any(
        row["kind"] == "agent.observation_error" for row in registry.store.bus_tail(limit=500)
    )


async def test_dead_trusted_owner_missing_transcript_is_unavailable_not_quarantined(
    registry, tmp_path
):
    root, project, sid, _transcript = vibe_session(tmp_path)
    from theater import harness as harness_mod

    harness_mod.HARNESSES["vibe"] = VibeHarness(root=root)
    target_id = make_job(
        registry.store,
        registry,
        handle="dead-missing-vibe",
        target_cwd=str(project),
        session_id=sid,
        session_correlation=str(TranscriptProvenance.OPERATOR),
    )
    target = registry.get(target_id)
    target.transcript_location = str(tmp_path / "gone" / "messages.jsonl")
    target.transcript_domain = str(root.resolve())
    registry.store.upsert_participant(target)
    registry.mark_dead(target_id)
    observer = Observer(registry, harnesses={})

    result = await read_segment(
        "dead-missing-vibe",
        store=registry.store,
        registry=registry,
        cwd=str(tmp_path),
        observer=observer,
    )

    assert result["transcript"]["available"] is False
    assert result["transcript"]["error_code"] is None
    assert "retained for resume" in result["transcript"]["reason"]
    assert registry.get(target_id).session_id == sid
    assert not observer.transcript_identity_lost(target_id)


@pytest.mark.parametrize(
    "provenance",
    [TranscriptProvenance.PROVEN, TranscriptProvenance.OPERATOR],
)
async def test_job_segment_trusts_restored_location_after_restart(registry, tmp_path, provenance):
    """A short-lived recall source reopens persisted trusted provenance."""
    root, project, sid, transcript = vibe_session(tmp_path)
    from theater import harness as harness_mod

    harness_mod.HARNESSES["vibe"] = VibeHarness(root=root)
    append_records(transcript, {"role": "assistant", "content": str(provenance)})
    target_id = make_job(
        registry.store,
        registry,
        handle=f"restored-{provenance}",
        target_cwd=str(project),
        session_id=sid,
        session_correlation=str(provenance),
    )
    target = registry.get(target_id)
    target.transcript_location = str(transcript)
    registry.store.upsert_participant(target)

    result = await read_segment(
        f"restored-{provenance}",
        store=registry.store,
        registry=registry,
        cwd=str(tmp_path),
    )

    assert result["transcript"]["available"] is True
    assert result["transcript"]["events"][0]["text"] == str(provenance)


@pytest.mark.parametrize(
    "provenance",
    [TranscriptProvenance.PROVEN, TranscriptProvenance.OPERATOR],
)
async def test_job_segment_does_not_apply_restored_trust_without_location(
    registry, tmp_path, provenance
):
    """Persisted trusted provenance is scoped to its bound transcript path."""
    root, project, sid, transcript = vibe_session(tmp_path)
    from theater import harness as harness_mod

    harness_mod.HARNESSES["vibe"] = VibeHarness(root=root)
    append_records(transcript, {"role": "assistant", "content": "same id, unbound path"})
    make_job(
        registry.store,
        registry,
        handle=f"unbound-{provenance}",
        target_cwd=str(project),
        session_id=sid,
        session_correlation=str(provenance),
    )

    result = await read_segment(
        f"unbound-{provenance}",
        store=registry.store,
        registry=registry,
        cwd=str(tmp_path),
    )

    assert result["transcript"]["available"] is False
    assert result["transcript"]["error_code"] == "transcript_correlation_untrusted"


async def test_job_segment_missing_transcript_still_returns_metadata(registry, tmp_path):
    """A job whose transcript no longer exists returns everything the
    database still remembers, with an explicit marker that the
    transcript is unavailable.

    It must not raise or return an empty dict when recorded paths are gone.
    """
    # Point the vibe observer at a root that has no sessions at all,
    # so find_transcript returns None and history() comes back empty.
    from theater import harness as harness_mod

    empty_root = tmp_path / "empty_logs" / "session"
    empty_root.mkdir(parents=True)
    vibe = VibeHarness(root=empty_root)
    harness_mod.HARNESSES["vibe"] = vibe

    make_job(
        registry.store,
        registry,
        handle="codex-dead",
        harness="vibe",
        target_cwd=str(tmp_path / "nowhere"),
    )
    add_touch(
        registry.store,
        handle="codex-dead",
        path="gone.py",
        sha_before="ccc",
        sha_after=None,
    )

    result = await read_segment(
        "codex-dead",
        store=registry.store,
        registry=registry,
        cwd=str(tmp_path),
    )

    # Must not raise, must not be empty.
    assert result["kind"] == "job"
    assert result["handle"] == "codex-dead"
    assert result["task"] == "do the thing"
    assert result["outcome"] == "done"
    assert result["paths"][0]["path"] == "gone.py"
    assert result["paths"][0]["sha_after"] is None
    # The transcript is explicitly unavailable, with a reason.
    assert result["transcript"]["available"] is False
    assert "reason" in result["transcript"]
    assert "no longer" in result["transcript"]["reason"]


async def test_job_segment_filters_legacy_touch_paths(registry, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    make_job(registry.store, registry, handle="filtered", target_cwd=str(project))
    add_touch(registry.store, handle="filtered", path="/tmp/secret", sha_before="a", sha_after="b")
    add_touch(registry.store, handle="filtered", path="../escape", sha_before="a", sha_after="b")
    add_touch(registry.store, handle="filtered", path="safe.py", sha_before="a", sha_after="b")

    result = await read_segment(
        "filtered", store=registry.store, registry=registry, cwd=str(tmp_path)
    )

    assert [path["path"] for path in result["paths"]] == ["safe.py"]


async def test_job_segment_with_no_target_returns_metadata_only(registry, tmp_path):
    """A job whose target_id is None (a CLI spawn with no target) has
    no transcript to read. The metadata is still there."""
    registry.store.create_job(
        type(
            "J",
            (),
            {
                "handle": "orphan",
                "caller_id": "caller",
                "target_id": None,
                "kind": "spawn",
                "prompt": "do something",
                "state": "done",
                "result": "ok",
                "error_code": None,
                "created_at": 1000.0,
                "finished_at": 1001.0,
            },
        )()
    )

    result = await read_segment(
        "orphan", store=registry.store, registry=registry, cwd=str(tmp_path)
    )

    assert result["kind"] == "job"
    assert result["handle"] == "orphan"
    assert result["transcript"]["available"] is False
    assert "no target" in result["transcript"]["reason"]


async def test_job_segment_clips_older_events_under_the_response_budget(
    registry, tmp_path, monkeypatch
):
    """An oversized job transcript is bounded by the response budget.

    Agent MCP bridges cap a single JSON-RPC frame (the stock Pi bridge at
    1 MiB) and hard-fail the whole connection on one oversized line, so the
    brief keeps the newest events and records the truncation instead of
    returning everything the database remembers. The budget is patched
    down to keep the fixture small; the arithmetic is budget-agnostic.
    """
    from theater.daemon import recall_read as recall_read_mod

    monkeypatch.setattr(recall_read_mod, "RECALL_READ_RESPONSE_MAX_BYTES", 2048)
    root, project, sid, transcript = vibe_session(tmp_path)
    from theater import harness as harness_mod

    harness_mod.HARNESSES["vibe"] = VibeHarness(root=root)
    for index in range(40):
        append_records(
            transcript,
            {"role": "assistant", "content": f"event {index:02d} " + "x" * 100},
        )
    make_job(
        registry.store,
        registry,
        handle="vibe-big",
        target_cwd=str(project),
        session_id=sid,
    )

    result = await read_segment(
        "vibe-big", store=registry.store, registry=registry, cwd=str(tmp_path)
    )

    assert result["transcript"]["available"] is True
    assert result["transcript"]["truncated"] is True
    kept = result["transcript"]["events"]
    assert 0 < len(kept) < 40
    assert result["transcript"]["dropped_events"] == 40 - len(kept)
    assert "read_transcript" in result["transcript"]["truncation_note"]
    # The newest events survive; the oldest were the ones clipped away.
    texts = [event["text"] for event in kept]
    assert "event 39 " in texts[-1]
    assert len(json.dumps(result).encode("utf-8")) <= 2048


async def test_job_segment_clips_an_oversized_single_event(registry, tmp_path, monkeypatch):
    """One event larger than the whole budget is text-clipped, not dropped.

    The brief still explains the job: the newest event stays, its text cut
    at a UTF-8 boundary and marked, the response inside the budget.
    """
    from theater.daemon import recall_read as recall_read_mod

    monkeypatch.setattr(recall_read_mod, "RECALL_READ_RESPONSE_MAX_BYTES", 2048)
    root, project, sid, transcript = vibe_session(tmp_path)
    from theater import harness as harness_mod

    harness_mod.HARNESSES["vibe"] = VibeHarness(root=root)
    append_records(
        transcript,
        {"role": "user", "content": "small question"},
        {"role": "assistant", "content": "huge " + "é" * 2000},
    )
    make_job(
        registry.store,
        registry,
        handle="vibe-huge",
        target_cwd=str(project),
        session_id=sid,
    )

    result = await read_segment(
        "vibe-huge", store=registry.store, registry=registry, cwd=str(tmp_path)
    )

    assert result["transcript"]["truncated"] is True
    kept = result["transcript"]["events"]
    assert len(kept) == 1
    assert kept[0]["text_clipped"] is True
    assert kept[0]["text"].endswith("…[clipped]")
    assert result["transcript"]["dropped_events"] == 1
    assert len(json.dumps(result).encode("utf-8")) <= 2048


async def test_job_segment_reports_the_parent_of_the_editing_session(registry, tmp_path):
    """The brief names whoever spawned the agent that ran the job.

    ``recall`` gives the lineage of a timeline point; opening that point
    must agree with it, so the two tools can be read together without
    the caller cross-referencing ids by hand.
    """
    project = tmp_path / "project"
    project.mkdir()
    pid = make_job(registry.store, registry, handle="h-child", target_cwd=str(project))
    registry.store.reparent_participant(pid, new_parent_id="boss")

    result = await read_segment(
        "h-child", store=registry.store, registry=registry, cwd=str(tmp_path)
    )

    assert result["parent_id"] == "boss"
