"""Pi usage accounting across restart, resume and rotation."""

from __future__ import annotations

import asyncio
import json

from tests.support.pi_transcripts import (
    append,
    mark_switch,
    message_record,
    session_record,
)
from theater.daemon.store import Store
from theater.harness.builtin.plugins.pi.observer import PiObserver
from theater.harness.contracts.events import TokenUsage
from theater.models import Participant
from theater.pricing import usage_cost_microcents
from theater.resume_floor import UNKNOWN_FLOOR, encode_floor
from theater.trajectory.enums import (
    CostProvenance,
    TrajectoryStatus,
)


def test_pi_summary_usage_is_durable_and_inherits_the_active_model(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    model_change = observer.parse_record(
        json.dumps(
            {
                "type": "model_change",
                "id": "model-1",
                "modelId": "gpt-5.6",
                "provider": "openai",
            }
        ),
        1,
    )
    assert model_change.trajectory[0].status is TrajectoryStatus.COMPLETED

    summary = observer.parse_record(
        json.dumps(
            {
                "type": "compaction",
                "id": "compact-1",
                "summary": "compacted",
                "usage": {"input": 12, "output": 3, "cost": {"total": 0.01}},
            }
        ),
        2,
    )

    assert len(summary.events) == 1
    assert summary.events[0].usage_only is True
    assert summary.events[0].usage is not None
    assert summary.events[0].usage.idempotency_key == "compact-1"
    assert summary.events[0].usage.model == "gpt-5.6"
    assert summary.events[0].usage.provider == "openai"
    assert all(fact.status is TrajectoryStatus.COMPLETED for fact in summary.trajectory)

    branch = observer.parse_record(
        json.dumps(
            {
                "type": "branch_summary",
                "id": "branch-1",
                "summary": "branched",
                "usage": {"input": 4, "output": 1},
            }
        ),
        3,
    )
    assert branch.events[0].usage_only is True
    assert branch.events[0].usage is not None
    assert branch.events[0].usage.input_tokens == 4
    assert all(fact.status is TrajectoryStatus.COMPLETED for fact in branch.trajectory)


def test_pi_cold_isolated_source_replays_pre_attach_usage_without_an_attach_completion(
    tmp_path,
) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        message_record(
            "assistant-1",
            {
                "role": "assistant",
                "model": "gpt-5.6",
                "content": "done",
                "stopReason": "stop",
                "usage": {"input": 11, "output": 2},
            },
        ),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )

    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.skipped == 0
    assert attached.last_event is None
    source.commit_attachment()

    batch = asyncio.run(source.read())
    usage = [event.usage for event in batch.events if event.usage is not None]
    assert [event.input_tokens for event in usage] == [11]


def test_pi_resumed_source_accepts_a_legacy_floor_checkpoint(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        message_record(
            "assistant-before",
            {
                "role": "assistant",
                "content": "old",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    observer = PiObserver(root=sessions, isolated=True)
    floor = observer.stream_floor(str(transcript))
    assert floor is not None
    append(
        transcript,
        message_record(
            "assistant-after",
            {
                "role": "assistant",
                "content": "new",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 2},
            },
        ),
    )
    source = observer.open_source(
        cwd=str(workdir), session_id="native-id", source_checkpoint=encode_floor(floor)
    )

    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.skipped == floor.records
    assert attached.last_event is None
    source.commit_attachment()

    batch = asyncio.run(source.read())
    usage = [event.usage for event in batch.events if event.usage is not None]
    assert [event.input_tokens for event in usage] == [7]


def test_pi_unknown_resume_checkpoint_fails_closed(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        message_record(
            "assistant-old",
            {
                "role": "assistant",
                "content": "old",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id", source_checkpoint=UNKNOWN_FLOOR
    )

    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.skipped == 2
    source.commit_attachment()
    assert asyncio.run(source.read()).events == ()


def test_pi_restart_replays_only_usage_from_a_known_isolated_transcript(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        message_record(
            "assistant-1",
            {
                "role": "assistant",
                "content": "done",
                "stopReason": "stop",
                "usage": {"input": 5, "output": 1},
            },
        ),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id", known_location=str(transcript)
    )

    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.last_event is None
    source.commit_attachment()

    batch = asyncio.run(source.read())
    assert len(batch.events) == 1
    assert batch.events[0].usage_only is True
    assert batch.events[0].usage is not None
    assert batch.events[0].usage.input_tokens == 5
    assert batch.trajectory == ()


def test_pi_restart_usage_reconciliation_is_idempotent_in_the_durable_ledger(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        message_record(
            "assistant-1",
            {
                "role": "assistant",
                "content": "done",
                "stopReason": "stop",
                "usage": {"input": 5, "output": 1},
            },
        ),
    )

    first = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(first.read()).attached is not None
    first.commit_attachment()
    initial = asyncio.run(first.read())

    restarted = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id", known_location=str(transcript)
    )
    assert asyncio.run(restarted.read()).attached is not None
    restarted.commit_attachment()
    catchup = asyncio.run(restarted.read())

    store = Store(tmp_path / "theater.db")
    try:
        participant = Participant(id="participant", harness="pi", session_id="native-id")
        store.upsert_participant(participant)
        for batch in (initial, catchup):
            for event in batch.events:
                if event.usage is None:
                    continue
                usage = event.usage
                assert store.record_usage(
                    participant_id=participant.id,
                    tree_root_id=participant.id,
                    usage_key=f"{participant.session_id}:{usage.idempotency_key}",
                    ts=event.ts or 0,
                    model=usage.model,
                    harness="pi",
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_creation_input_tokens=usage.cache_creation_input_tokens,
                    cache_read_input_tokens=usage.cache_read_input_tokens,
                    reasoning_output_tokens=usage.reasoning_output_tokens,
                    cost_microcents=0,
                ) is (batch is initial)
        assert store.usage_totals()["input_tokens"] == 5
    finally:
        store.close()


def test_pi_restart_resumes_from_the_current_source_checkpoint(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        message_record(
            "assistant-1",
            {
                "role": "assistant",
                "content": "one",
                "stopReason": "stop",
                "usage": {"input": 5, "output": 1},
            },
        ),
    )
    first = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(first.read()).attached is not None
    first.commit_attachment()
    assert [
        event.usage.input_tokens for event in asyncio.run(first.read()).events if event.usage
    ] == [5]
    first.acknowledge_source_checkpoint()
    checkpoint = first.source_checkpoint()
    assert checkpoint is not None

    append(
        transcript,
        message_record(
            "assistant-2",
            {
                "role": "assistant",
                "content": "two",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    restarted = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir),
        session_id="native-id",
        known_location=str(transcript),
        source_checkpoint=checkpoint,
    )
    attached = asyncio.run(restarted.read()).attached
    assert attached is not None
    assert attached.last_event is None
    restarted.commit_attachment()
    batch = asyncio.run(restarted.read())
    assert [event.usage.input_tokens for event in batch.events if event.usage] == [7]
    assert all(event.usage_only for event in batch.events)
    assert batch.trajectory == ()


def test_pi_resume_new_restart_replays_only_the_post_floor_usage(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    old = sessions / "old.jsonl"
    append(
        old,
        session_record(session_id="old", cwd=workdir, timestamp="2020-01-01T00:00:00.000Z"),
        message_record(
            "old-a",
            {
                "role": "assistant",
                "content": "old",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    observer = PiObserver(root=sessions, isolated=True)
    floor = observer.stream_floor(str(old))
    assert floor is not None
    source = observer.open_source(
        cwd=str(workdir),
        session_id="old",
        after=1_800_000_000,
        source_checkpoint=encode_floor(floor),
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()
    assert asyncio.run(source.read()).events == ()

    fresh = sessions / "fresh.jsonl"
    append(
        fresh,
        session_record(session_id="fresh", cwd=workdir, timestamp="2028-01-01T00:00:00.000Z"),
        message_record(
            "new-a",
            {
                "role": "assistant",
                "content": "new",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    mark_switch(sessions, previous=old, target=fresh, reason="new")
    rotated = asyncio.run(source.refresh())
    assert rotated.attached is not None
    source.commit_attachment()
    assert [
        event.usage.input_tokens for event in asyncio.run(source.read()).events if event.usage
    ] == [7]
    source.acknowledge_source_checkpoint()
    checkpoint = source.source_checkpoint()
    assert checkpoint is not None

    append(
        fresh,
        message_record(
            "new-b",
            {
                "role": "assistant",
                "content": "new after restart",
                "stopReason": "stop",
                "usage": {"input": 9, "output": 1},
            },
        ),
    )
    restarted = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir),
        session_id="fresh",
        known_location=str(fresh),
        source_checkpoint=checkpoint,
        after=1_800_000_000,
    )
    assert asyncio.run(restarted.read()).attached is not None
    restarted.commit_attachment()
    restarted_batch = asyncio.run(restarted.read())
    assert [event.usage.input_tokens for event in restarted_batch.events if event.usage] == [9]
    assert all(event.usage_only for event in restarted_batch.events)
    assert restarted_batch.trajectory == ()

    append(
        old,
        message_record(
            "old-b",
            {
                "role": "assistant",
                "content": "old again",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    assert asyncio.run(source.refresh()).attached is None


def test_pi_retries_an_unacknowledged_usage_batch_before_advancing(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()

    append(
        transcript,
        message_record(
            "lost",
            {
                "role": "assistant",
                "content": "must retry",
                "stopReason": "stop",
                "usage": {"input": 5, "output": 1},
            },
        ),
    )
    failed = asyncio.run(source.read())
    assert [event.usage.input_tokens for event in failed.events if event.usage] == [5]

    source.rollback_source_checkpoint()
    append(
        transcript,
        message_record(
            "later",
            {
                "role": "assistant",
                "content": "later",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    retried = asyncio.run(source.read())
    assert [event.usage.input_tokens for event in retried.events if event.usage] == [5, 7]


def test_pi_rejected_rotation_keeps_all_active_stream_state(tmp_path, monkeypatch) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    old = sessions / "old.jsonl"
    append(old, session_record(session_id="old", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="old"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()

    append(
        old,
        message_record(
            "old-a",
            {
                "role": "assistant",
                "content": "old",
                "stopReason": "stop",
                "usage": {"input": 5, "output": 1},
            },
        ),
    )
    asyncio.run(source.read())
    source.acknowledge_source_checkpoint()

    fresh = sessions / "fresh.jsonl"
    append(
        fresh,
        session_record(session_id="fresh", cwd=workdir),
        message_record(
            "candidate-history",
            {
                "role": "assistant",
                "content": "x" * 2_000,
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    mark_switch(sessions, previous=old, target=fresh, reason="new")
    monkeypatch.setattr(source, "_replay_cursor", lambda *_args, **_kwargs: (0, 0, True))
    rotation = asyncio.run(source.refresh())
    assert rotation.attached is not None
    source.discard_attachment()

    append(
        old,
        message_record(
            "old-b",
            {
                "role": "assistant",
                "content": "still old",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    continued = asyncio.run(source.read())
    assert [event.text for event in continued.events if event.text] == ["still old"]
    assert [event.usage.input_tokens for event in continued.events if event.usage] == [7]
    assert all(not event.usage_only for event in continued.events)
    source.acknowledge_source_checkpoint()
    checkpoint = source.source_checkpoint()
    assert checkpoint is not None

    append(
        old,
        message_record(
            "old-c",
            {
                "role": "assistant",
                "content": "after restart",
                "stopReason": "stop",
                "usage": {"input": 9, "output": 1},
            },
        ),
    )
    restarted = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir),
        session_id="old",
        known_location=str(old),
        source_checkpoint=checkpoint,
    )
    assert asyncio.run(restarted.read()).attached is not None
    restarted.commit_attachment()
    assert [
        event.usage.input_tokens for event in asyncio.run(restarted.read()).events if event.usage
    ] == [9]


def _assistant_usage(*, cost: float) -> TokenUsage:
    parsed = PiObserver().parse_record(
        json.dumps(
            {
                "type": "message",
                "id": "assistant-1",
                "timestamp": "2026-08-31T12:00:00Z",
                "message": {
                    "role": "assistant",
                    "provider": "mistral",
                    "model": "zai-glm-5-2",
                    "content": [{"type": "text", "text": "done"}],
                    "stopReason": "stop",
                    "usage": {
                        "input": 1_000,
                        "output": 100,
                        "cacheRead": 200,
                        "cacheWrite": 300,
                        "cost": {"total": cost},
                    },
                },
            }
        ),
        1,
    )
    return next(event.usage for event in parsed.events if event.usage is not None)


def test_pi_zero_placeholder_cost_falls_back_to_catalog_estimate() -> None:
    usage = _assistant_usage(cost=0)

    assert usage.cost_usd is None
    assert usage.cost_provenance is CostProvenance.UNKNOWN
    assert usage_cost_microcents(usage) > 0


def test_pi_positive_reported_cost_remains_authoritative() -> None:
    usage = _assistant_usage(cost=0.25)

    assert usage.cost_usd == 0.25
    assert usage.cost_provenance is CostProvenance.REPORTED
    assert usage_cost_microcents(usage) == 25_000_000
