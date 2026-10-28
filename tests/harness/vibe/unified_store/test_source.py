"""Vibe unified source: projection, checkpoints, turn boundaries, history order and lineage."""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from tests.support.paths import fixture
from tests.support.vibe_store import (
    A_ENDS,
    ANSWER_A,
    B_STARTS,
    GEN1,
    GEN2,
    RUNNING_A,
    SESSION_ID,
    USER_A,
    USER_B,
    Store,
    attached_to_running_a,
    make_state,
    public_effect,
    public_message,
    source_state,
    turn_doc,
    turn_record,
)
from theater.daemon.trajectory.project import fact_to_record
from theater.harness.builtin.plugins.vibe.manifest import _vibe_stream_floor
from theater.harness.builtin.plugins.vibe.observer import VibeObserver
from theater.harness.contracts.callbacks import StreamFloorContext
from theater.harness.contracts.events import EventKind, TurnTerminal
from theater.provenance import TranscriptProvenance
from theater.trajectory.enums import TrajectoryKind, TrajectoryStatus
from theater.trajectory.grouping import deterministic_record_order, merge_records

GEN3 = "0" * 15 + "3"


def test_source_projects_mutation_and_resumes_from_checkpoint(store: Store) -> None:
    user = public_message("user-1", "user", "question")
    partial = public_message("assistant-1", "assistant", "draft", status="in_progress")
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=source_state([user, partial], turn_status="in_progress"),
        watermark=1,
    )
    observer = VibeObserver(root=store.session_root.parent.parent)
    source = observer.open_source(cwd="/tmp/work")
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    assert attached.attached.point is not None and attached.attached.point.position == 1
    source.commit_attachment()
    source.acknowledge_source_checkpoint()

    completed = public_message("assistant-1", "assistant", "answer", updated_at=3_000)
    store.publish(
        generation=GEN2,
        snapshot_sequence=1,
        state=source_state([user, completed], turn_status="completed"),
        watermark=2,
    )
    update = asyncio.run(source.read())
    assert [
        (event.kind, event.text, event.turn_end, event.turn_terminal) for event in update.events
    ] == [(EventKind.ASSISTANT, "answer", True, TurnTerminal.COMPLETED)]
    assert len(update.trajectory) == 1
    assert update.trajectory[0].native_id == "assistant-1"
    assert update.trajectory[0].revision == 2
    assert update.trajectory[0].status is TrajectoryStatus.COMPLETED
    source.acknowledge_source_checkpoint()
    checkpoint = source.source_checkpoint()
    assert checkpoint is not None
    checkpoint_data = json.loads(checkpoint)
    checkpoint_data["location"] = str(
        store.current.parent / ".." / store.current.parent.name / "CURRENT"
    )
    checkpoint = json.dumps(checkpoint_data)

    store.publish(
        generation=GEN3,
        snapshot_sequence=2,
        state=source_state([user, completed, public_effect()], turn_status="completed"),
        watermark=3,
    )
    resumed = observer.open_source(
        cwd="/tmp/work",
        session_id=SESSION_ID,
        session_provenance=TranscriptProvenance.EXACT,
        known_location=str(store.current),
        source_checkpoint=checkpoint,
    )
    assert resumed.source_checkpoint() == checkpoint
    assert asyncio.run(resumed.read()).attached is not None
    resumed.commit_attachment()
    assert resumed.source_checkpoint() == checkpoint
    resumed_update = asyncio.run(resumed.read())
    assert [event.kind for event in resumed_update.events] == [
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
    ]
    assert [fact.kind for fact in resumed_update.trajectory] == [
        TrajectoryKind.TOOL_CALL,
        TrajectoryKind.TOOL_RESULT,
    ]
    assert {fact.revision for fact in resumed_update.trajectory} == {3}
    resumed.acknowledge_source_checkpoint()

    retry = public_message("assistant-2", "assistant", "retry", status="in_progress")
    store.publish(
        generation="0" * 15 + "4",
        snapshot_sequence=3,
        state=source_state([user, completed, public_effect(), retry], turn_status="in_progress"),
        watermark=4,
    )
    assert not asyncio.run(resumed.read()).events
    resumed.acknowledge_source_checkpoint()
    failed = public_message("assistant-2", "assistant", "partial answer")
    failed_state = source_state([user, completed, public_effect(), failed], turn_status="failed")
    failed_state["latestTurn"]["error"] = {"message": "turn failed"}
    store.publish(
        generation="0" * 15 + "5",
        snapshot_sequence=4,
        state=failed_state,
        watermark=5,
    )
    failed_update = asyncio.run(resumed.read())
    assert [
        (event.kind, event.text, event.turn_end, event.turn_terminal)
        for event in failed_update.events
    ] == [
        (EventKind.ASSISTANT, "partial answer", False, None),
        (EventKind.ERROR, "turn failed", True, TurnTerminal.FAILED),
    ]
    resumed.acknowledge_source_checkpoint()
    store.current.unlink()
    assert asyncio.run(resumed.read()).error_code == "transcript_identity_lost"


async def test_source_orders_history_pages_and_live_revisions_by_entry_position(store: Store):
    entries = [
        public_message(f"message-{9 - index}", "assistant", str(index)) for index in range(6)
    ]
    effects = [
        {
            **public_effect(),
            "id": f"effect-{9 - index}",
            "detail": {"kind": "tool", "toolName": "mcp_theater.read_transcript", "input": {}},
        }
        for index in range(4)
    ]
    entries.extend(effects)
    store.publish(generation=GEN1, snapshot_sequence=100, state=make_state(entries), watermark=10)
    source = VibeObserver(root=store.session_root.parent.parent).open_source(cwd="/tmp/work")
    assert (await source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()
    records = ()
    before = None
    while True:
        page = await source.history_page(before=before, limit=4)
        assert page.error is None
        records = merge_records(
            records,
            [
                fact_to_record(fact, participant_id="p", source_epoch="epoch")
                for fact in page.trajectory
            ],
        )
        before = page.older_cursor
        if before is None:
            break
    ordered = deterministic_record_order(records)
    assert [record.raw_index for record in ordered] == [*range(6), 6, 6, 7, 7, 8, 8, 9, 9]
    assert [record.mcp_tool for record in ordered[-8:]] == ["read_transcript"] * 8
    assert all(record.source_offset == record.raw_index for record in ordered)

    updated = {**entries[1], "content": [{"type": "text", "text": "revised"}]}
    store.publish(
        generation=GEN2,
        snapshot_sequence=200,
        state=make_state([entries[0], updated, *entries[2:]]),
        watermark=11,
    )
    update = await source.read()
    assert [(fact.raw_index, fact.source_offset, fact.revision) for fact in update.trajectory] == [
        (1, 1, 11)
    ]
    revised = deterministic_record_order(
        merge_records(
            ordered,
            [
                fact_to_record(fact, participant_id="p", source_epoch="epoch")
                for fact in update.trajectory
            ],
        )
    )
    assert [record.record_id for record in revised] == [record.record_id for record in ordered]
    source.acknowledge_source_checkpoint()
    checkpoint = json.loads(source.source_checkpoint())
    assert (checkpoint["sequence"], checkpoint["watermark"]) == (200, 11)

    store.publish(
        generation=GEN3,
        snapshot_sequence=201,
        state=make_state([updated, *entries[2:]]),
        watermark=12,
    )
    shifted = await source.read()
    latest = await source.history_page(limit=20)
    assert len(shifted.trajectory) == 13
    assert {fact.native_id: fact.source_offset for fact in shifted.trajectory} == {
        fact.native_id: fact.source_offset for fact in latest.trajectory
    }
    assert all(fact.source_offset == fact.raw_index for fact in shifted.trajectory)
    assert not shifted.events


def test_interrupted_turn_boundary_carries_interrupted_terminal(store: Store) -> None:
    """An interrupted vibe turn answers its job as killed, not done."""
    user = public_message("user-1", "user", "question")
    partial = public_message("assistant-1", "assistant", "draft", status="in_progress")
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=source_state([user, partial], turn_status="in_progress"),
        watermark=1,
    )
    observer = VibeObserver(root=store.session_root.parent.parent)
    source = observer.open_source(cwd="/tmp/work")
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()

    store.publish(
        generation=GEN2,
        snapshot_sequence=1,
        state=source_state([user, partial], turn_status="interrupted"),
        watermark=2,
    )
    update = asyncio.run(source.read())
    boundaries = [event for event in update.events if event.turn_end]
    assert boundaries and boundaries[-1].turn_terminal is TurnTerminal.INTERRUPTED


def test_failed_turn_with_blank_message_names_the_failure(store: Store) -> None:
    """A model ReadTimeout leaves message empty; the crash result must still say why."""
    error = json.loads((fixture("vibe_failed_turn", "latest_turn_error.json")).read_text())
    user = public_message("user-1", "user", "question")
    narration = public_message("assistant-1", "assistant", "Let me look at the files.")
    state = source_state([user], turn_status="in_progress")
    store.publish(generation=GEN1, snapshot_sequence=0, state=state, watermark=1)
    source = VibeObserver(root=store.session_root.parent.parent).open_source(cwd="/tmp/work")
    asyncio.run(source.read())
    source.commit_attachment()
    source.acknowledge_source_checkpoint()

    failed = source_state([user, narration], turn_status="failed")
    failed["latestTurn"]["error"] = error
    store.publish(generation=GEN2, snapshot_sequence=1, state=failed, watermark=2)
    boundary = [event for event in asyncio.run(source.read()).events if event.turn_end][-1]
    assert boundary.turn_terminal is TurnTerminal.FAILED
    assert boundary.text == "model_stream_failed: ReadTimeout (mistral/zai-glm-5-3)"


ANSWER_B = public_message("asst-b", "assistant", "ANSWER B", turn_id="turn-b")


def ends(batch) -> list[tuple[EventKind, str, str | None, bool]]:
    return [
        (event.kind, event.text, event.turn_id, event.turn_end)
        for event in batch.events
        if not event.usage_only
    ]


def test_turns_folded_into_one_read_each_keep_their_end(store: Store) -> None:
    """A ends and B starts between two polls: A's end must still precede B's prompt."""
    source = attached_to_running_a(store)
    journal = [A_ENDS, B_STARTS]
    store.publish(
        generation=GEN1, snapshot_sequence=0, state=RUNNING_A, watermark=1, journal=journal
    )

    folded = asyncio.run(source.read())
    assert ends(folded) == [
        (EventKind.ASSISTANT, "ANSWER A", "turn-a", True),
        (EventKind.USER, "job B prompt", "turn-b", False),
    ]
    assert folded.turn_state is not None
    assert (folded.turn_state.turn_id, folded.turn_state.open) == ("turn-b", True)
    assert folded.turn_state.queued == ("q",)
    assert ("turn-b", "queue-turn-b") in folded.turn_state.origins
    source.acknowledge_source_checkpoint()
    unchanged = asyncio.run(source.read())
    assert unchanged.events == () and unchanged.turn_state is not None
    assert (unchanged.turn_state.turn_id, unchanged.turn_state.queued) == ("turn-b", ("q",))

    journal.append(turn_record(4, "turn-b", "completed", ANSWER_B))
    store.publish(
        generation=GEN1, snapshot_sequence=0, state=RUNNING_A, watermark=1, journal=journal
    )
    later = asyncio.run(source.read())
    assert ends(later) == [(EventKind.ASSISTANT, "ANSWER B", "turn-b", True)]
    assert later.turn_state is not None and not later.turn_state.open


def test_a_turn_end_folded_into_a_newer_generation_is_drained(store: Store) -> None:
    """A newer publication's snapshot folds A's end away; the old journal tail keeps it."""
    source = attached_to_running_a(store)
    store.publish(
        generation=GEN1, snapshot_sequence=0, state=RUNNING_A, watermark=1, journal=[A_ENDS]
    )
    store.publish(
        generation=GEN2,
        snapshot_sequence=1,
        state=turn_doc([USER_A, ANSWER_A, USER_B], "turn-b", "in_progress"),
        watermark=3,
    )

    assert ends(asyncio.run(source.read())) == [
        (EventKind.ASSISTANT, "ANSWER A", "turn-a", True),
        (EventKind.USER, "job B prompt", "turn-b", False),
    ]


def test_republication_keeps_turn_ends_after_their_manifest_is_collected(store: Store) -> None:
    history = [
        public_message(f"{role}-{turn}", role, "history", turn_id=f"t{turn}")
        for turn in range(1, 34)
        for role in ("user", "assistant")
    ]
    generation = f"{661:016d}"
    state = turn_doc(history, "t33", "completed")
    store.publish(generation=generation, snapshot_sequence=660, state=state, watermark=200)
    source = VibeObserver(root=store.session_root.parent.parent).open_source(cwd="/tmp/work")
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()

    user = public_message("user-34", "user", "resume", turn_id="t34")
    answer = public_message("assistant-34", "assistant", "resumed", turn_id="t34")
    store.publish(
        generation=generation,
        snapshot_sequence=660,
        state=state,
        watermark=200,
        journal=[
            ("core_input", {}),
            turn_record(201, "t34", "in_progress", user),
            ("core_input", {}),
            ("core_input", {}),
            turn_record(202, "t34", "completed", answer),
            ("core_input", {}),
        ],
    )
    for newer in (667, 668):
        store.publish(
            generation=f"{newer:016d}",
            snapshot_sequence=666,
            state=turn_doc([*history, user, answer], "t34", "completed"),
            watermark=202,
        )
    # Vibe retains generations and journals independently, keeping the newest two of each.
    shutil.rmtree(store.generation_dir(generation))
    assert (store.session_root / "journal" / f"{661:016d}.jsonl").exists()

    batch = asyncio.run(source.read())
    assert batch.turn_state is not None and batch.turn_state.complete
    assert [end for end in ends(batch) if end[3]] == [(EventKind.ASSISTANT, "resumed", "t34", True)]


def test_a_fold_after_a_generation_published_at_the_read_point_keeps_its_end(store: Store):
    """Vibe publishes a generation right after a turn ends; the next fold must survive it."""
    source = attached_to_running_a(store)
    store.publish(
        generation=GEN1, snapshot_sequence=0, state=RUNNING_A, watermark=1, journal=[A_ENDS]
    )
    asyncio.run(source.read())
    source.acknowledge_source_checkpoint()
    settled = turn_doc([USER_A, ANSWER_A], "turn-a", "completed")
    store.publish(generation=GEN2, snapshot_sequence=1, state=settled, watermark=2)
    assert asyncio.run(source.read()).events == ()

    journal = [
        turn_record(3, "turn-b", "in_progress", USER_B),
        turn_record(4, "turn-b", "completed", ANSWER_B),
        turn_record(5, "turn-c", "in_progress"),
    ]
    store.publish(generation=GEN2, snapshot_sequence=1, state=settled, watermark=2, journal=journal)
    assert ends(asyncio.run(source.read())) == [
        (EventKind.USER, "job B prompt", "turn-b", False),
        (EventKind.ASSISTANT, "ANSWER B", "turn-b", True),
    ]


def test_one_turn_id_proves_its_end_after_its_journal_is_collected(store: Store) -> None:
    """Vibe keeps two journals: after a turn end and a later fold, A's is gone and A still ended."""
    source = attached_to_running_a(store)
    store.publish(
        generation=GEN1, snapshot_sequence=0, state=RUNNING_A, watermark=1, journal=[A_ENDS]
    )
    settled = turn_doc([USER_A, ANSWER_A], "turn-a", "completed")
    store.publish(
        generation=GEN2,
        snapshot_sequence=1,
        state=settled,
        watermark=2,
        journal=[turn_record(3, "turn-a", "completed")],
    )
    store.publish(generation=GEN3, snapshot_sequence=2, state=settled, watermark=3)
    shutil.rmtree(store.generation_dir(GEN1))
    (store.session_root / "journal" / f"{1:016d}.jsonl").unlink()

    batch = asyncio.run(source.read())
    assert batch.turn_state is not None and batch.turn_state.complete
    assert [end for end in ends(batch) if end[3]] == [
        (EventKind.ASSISTANT, "ANSWER A", "turn-a", True)
    ]


@pytest.mark.parametrize(
    ("gap", "previous_busy", "current_open", "complete"),
    [
        (2, False, False, True),
        (4, False, False, False),
        (2, True, False, False),
        (3, False, True, False),
    ],
)
def test_a_short_closed_gap_holds_only_the_current_views_turn(
    store: Store, gap: int, previous_busy: bool, current_open: bool, complete: bool
) -> None:
    """No work open in either view and a gap of three publishes: no other turn can fit."""
    settled = turn_doc([USER_A, ANSWER_A], "turn-a", "completed")
    previous = turn_doc([USER_A, ANSWER_A], "turn-a", "completed")
    if previous_busy:  # a pending pre-agent hook turns the session busy behind a terminal turn
        previous["session"]["status"] = {"type": "running"}
    store.publish(generation=GEN1, snapshot_sequence=0, state=previous, watermark=1)
    source = VibeObserver(root=store.session_root.parent.parent).open_source(cwd="/tmp/work")
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()

    end = 1 + gap  # the watermark the current view reaches
    records = [turn_record(end, "turn-b", "in_progress", USER_B)]
    if not current_open:
        records = [
            turn_record(end - 1, "turn-b", "in_progress", USER_B),
            turn_record(end, "turn-b", "completed", ANSWER_B),
        ]
    store.publish(
        generation=GEN2,
        snapshot_sequence=1,
        state=settled,
        watermark=end - 1,
        journal=records,
    )
    (store.session_root / "journal" / f"{1:016d}.jsonl").unlink()

    batch = asyncio.run(source.read())
    assert batch.turn_state is not None and batch.turn_state.complete is complete
    if complete:
        assert ends(batch) == [
            (EventKind.USER, "job B prompt", "turn-b", False),
            (EventKind.ASSISTANT, "ANSWER B", "turn-b", True),
        ]


def test_a_restored_checkpoint_replays_the_turn_ends_behind_it(store: Store) -> None:
    """A restart resumes from its checkpoint; A's end, folded after it, must still be read."""
    checkpoint = attached_to_running_a(store).source_checkpoint()
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=RUNNING_A,
        watermark=1,
        journal=[A_ENDS, B_STARTS],
    )
    resumed = VibeObserver(root=store.session_root.parent.parent).open_source(
        cwd="/tmp/work",
        session_id=SESSION_ID,
        session_provenance=TranscriptProvenance.EXACT,
        known_location=str(store.current),
        source_checkpoint=checkpoint,
    )
    assert asyncio.run(resumed.read()).attached is not None
    resumed.commit_attachment()
    resumed.acknowledge_source_checkpoint()

    replayed = asyncio.run(resumed.read())
    assert ends(replayed) == [
        (EventKind.ASSISTANT, "ANSWER A", "turn-a", True),
        (EventKind.USER, "job B prompt", "turn-b", False),
    ]
    assert replayed.turn_state is not None and replayed.turn_state.complete


@pytest.mark.parametrize("retained", [True, False])
def test_turn_ends_across_several_generations_replay_every_retained_segment(
    store: Store, retained: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """GEN2's journal alone holds B's end; without it the read must say so, never claim none."""
    source = attached_to_running_a(store)
    store.publish(
        generation=GEN1, snapshot_sequence=0, state=RUNNING_A, watermark=1, journal=[A_ENDS]
    )
    store.publish(
        generation=GEN2,
        snapshot_sequence=1,
        state=turn_doc([USER_A, ANSWER_A], "turn-a", "completed"),
        watermark=2,
        journal=[
            turn_record(3, "turn-b", "in_progress", USER_B),
            turn_record(4, "turn-b", "completed", ANSWER_B),
            turn_record(5, "turn-c", "in_progress"),
        ],
    )
    store.publish(
        generation=GEN3,
        snapshot_sequence=4,
        state=turn_doc([USER_A, ANSWER_A, USER_B, ANSWER_B], "turn-c", "in_progress"),
        watermark=5,
    )
    if not retained:
        shutil.rmtree(store.generation_dir(GEN2))
        (store.session_root / "journal" / f"{2:016d}.jsonl").unlink()

    batch = asyncio.run(source.read())
    assert batch.turn_state is not None and batch.turn_state.complete is retained
    assert [end for end in ends(batch) if end[3]] == (
        [
            (EventKind.ASSISTANT, "ANSWER A", "turn-a", True),
            (EventKind.ASSISTANT, "ANSWER B", "turn-b", True),
        ]
        if retained
        else []
    )
    if not retained:
        assert any(
            record.levelname == "WARNING" and "retained journals: [1, 5]" in record.getMessage()
            for record in caplog.records
        )
        source.rollback_source_checkpoint()
        retried = asyncio.run(source.read())
        assert retried.turn_state is not None and not retried.turn_state.complete
        assert [end for end in ends(retried) if end[3]] == []
        source.acknowledge_source_checkpoint()
        settled = asyncio.run(source.read())
        assert settled.turn_state is not None and settled.turn_state.complete


def test_expired_checkpoint_rebaselines_without_replaying_entries(store: Store) -> None:
    state = source_state([public_message("user-1", "user", "question")], turn_status="completed")
    store.publish(generation=GEN1, snapshot_sequence=0, state=state, watermark=1)
    observer = VibeObserver(root=store.session_root.parent.parent)
    source = observer.open_source(cwd="/tmp/work")
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()
    checkpoint = source.source_checkpoint()

    store.publish(
        generation=GEN2,
        snapshot_sequence=2,
        state=source_state(
            [*state["history"]["entries"], public_message("assistant-1", "assistant", "answer")],
            turn_status="completed",
        ),
        watermark=2,
    )
    resumed = observer.open_source(
        cwd="/tmp/work", known_location=str(store.current), source_checkpoint=checkpoint
    )
    checkpoint_file = store.generation_dir(GEN1) / "checkpoint.json"
    checkpoint_file.write_bytes(b"{}\n")
    corrupt = asyncio.run(resumed.read())
    assert corrupt.waiting and corrupt.error_code == "vibe_unified_store_invalid"

    shutil.rmtree(store.generation_dir(GEN1))
    attached = asyncio.run(resumed.read())
    assert attached.attached is not None
    # The attachment itself, not a later read, says the skipped history is unprovable.
    assert attached.turn_state is not None and not attached.turn_state.complete
    resumed.commit_attachment()
    resumed.acknowledge_source_checkpoint()
    assert asyncio.run(resumed.read()).error_code == "vibe_unified_checkpoint_expired"
    assert asyncio.run(resumed.read()).events == ()


def test_a_journal_moving_backwards_keeps_the_acknowledged_open_turn(store: Store) -> None:
    """Truncated under acknowledged open B: no older closed A, no checkpoint moved back."""
    source = attached_to_running_a(store)
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=RUNNING_A,
        watermark=1,
        journal=[A_ENDS, B_STARTS],
    )
    asyncio.run(source.read())
    source.acknowledge_source_checkpoint()
    acknowledged = source.source_checkpoint()
    store.publish(
        generation=GEN1, snapshot_sequence=0, state=RUNNING_A, watermark=1, journal=[A_ENDS]
    )

    regressed = asyncio.run(source.read())

    assert regressed.error_code == "vibe_unified_store_invalid" and regressed.events == ()
    assert regressed.turn_state is not None
    assert (regressed.turn_state.turn_id, regressed.turn_state.open) == ("turn-b", True)
    assert not regressed.turn_state.complete
    assert source.pending_source_checkpoint() is None
    assert source.source_checkpoint() == acknowledged


def test_history_cursor_pages_fresh_source_without_attaching(store: Store) -> None:
    messages = [public_message(f"message-{index}", "user", f"text-{index}") for index in range(3)]
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=source_state(messages, turn_status="completed"),
        watermark=3,
    )
    source = VibeObserver(root=store.session_root.parent.parent).open_source(cwd="/tmp/work")
    newest = asyncio.run(source.history_page(limit=1))
    older = asyncio.run(source.history_page(before=newest.older_cursor, limit=1))

    assert [fact.kind for fact in newest.trajectory] == [TrajectoryKind.USAGE]
    assert newest.events == ()
    assert newest.older_cursor is not None
    assert older.error_code is None
    assert [event.text for event in older.events] == ["text-2"]


def test_exact_malformed_store_does_not_fall_back_to_legacy(store: Store) -> None:
    legacy = store.session_root.parent.parent / "session_20260910_000000_sess"
    legacy.mkdir()
    (legacy / "messages.jsonl").write_text('{"role":"user","content":"legacy"}\n')
    store.current.write_text("{")
    observer = VibeObserver(root=store.session_root.parent.parent)

    assert observer.find_transcript(cwd="/tmp/work", session_id=SESSION_ID) == store.current
    batch = asyncio.run(observer.open_source(cwd="/tmp/work", session_id=SESSION_ID).read())
    assert batch.error_code == "vibe_unified_store_invalid"
    with pytest.raises(ValueError, match="Unified Vibe candidate is invalid"):
        observer.admit_operator_candidate(cwd="/tmp/work", candidate=str(store.current))

    store.current.unlink()
    missing = asyncio.run(
        observer.open_source(
            cwd="/tmp/work",
            session_provenance=TranscriptProvenance.EXACT,
            known_location=str(store.current),
        ).read()
    )
    assert missing.error_code == "transcript_identity_lost"


def test_relocated_session_is_still_found_by_its_first_cwd(store: Store) -> None:
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=make_state([]),
        watermark=1,
        store_minor=9,
        session_metadata={"cwd": "/tmp/moved", "origin_cwd": "/tmp/work"},
    )
    observer = VibeObserver(root=store.session_root.parent.parent)

    assert observer.find_unified_transcript(cwd="/tmp/work") == store.current


def test_child_lineage_and_logical_floor(store: Store) -> None:
    child_id = "child-1"
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=make_state([public_effect(child_id)]),
        watermark=1,
    )
    child = Store(store.session_root.parent.parent, child_id)
    child.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=make_state([], session_id=child_id),
        watermark=1,
        parent_session_id=SESSION_ID,
    )
    observer = VibeObserver(root=store.session_root.parent.parent)

    assert observer.find_unified_transcript(cwd="/tmp/work") == store.current
    child_candidate = next(
        candidate
        for candidate in observer.transcript_candidates(cwd="/tmp/work")
        if candidate.session_id == child_id
    )
    assert child_candidate.rejection_reason is not None
    assert [item.session_id for item in observer.native_children(store.current)] == [child_id]
    floor = _vibe_stream_floor(StreamFloorContext(location=str(store.current)))
    assert floor is not None
    assert floor.records is None
    assert floor.stream_id is not None and floor.stream_id.startswith("vibe-unified:")
    assert floor.position == 1
