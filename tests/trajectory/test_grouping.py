"""Trajectory projection order and grouping: turns, steps, between-turn buckets and chunking."""

from __future__ import annotations

from tests.support.trajectory_records import make_record
from theater.constants.trajectory import (
    TRAJECTORY_MAX_GROUP_CHILDREN,
    TRAJECTORY_MAX_GROUP_RECORD_IDS,
)
from theater.daemon.trajectory.project import event_to_record
from theater.harness.contracts.events import Event, EventKind
from theater.trajectory import (
    GroupKind,
    Timing,
    TimingProvenance,
    deterministic_record_order,
    group_records,
    merge_records,
)
from theater.trajectory.identity import fallback_record_id


def test_projection_identity_revision_and_grouping_are_deterministic() -> None:
    assert fallback_record_id("trusted-epoch", 4, 2) == "trusted-epoch:4:2"
    event = Event(
        kind=EventKind.ASSISTANT, text="done", raw_index=4, turn_id="turn-1", turn_end=True
    )
    record = event_to_record(event, participant_id="p", source_epoch="trusted-epoch")
    assert record.record_id == "trusted-epoch:4:0"

    old = make_record("native", revision=1)
    new = make_record("native", revision=2)
    assert merge_records((new,), (old,)) == (new,)

    groups = group_records(
        (
            make_record("unplaced", raw_index=0),
            make_record("step", raw_index=1, turn_id="t1", step_id="s1"),
            make_record("turn", raw_index=2, turn_id="t1"),
        )
    )
    assert groups[0].kind is GroupKind.BETWEEN_TURNS
    assert groups[1].kind is GroupKind.TURN
    assert groups[1].children[0].kind is GroupKind.STEP


def test_cross_stream_order_does_not_use_lexical_epoch_order() -> None:
    records = (
        make_record("z-2", source_epoch="z", raw_index=2),
        make_record("a-1", source_epoch="a", raw_index=1),
        make_record("z-1", source_epoch="z", raw_index=1),
    )
    ordered = deterministic_record_order(records)
    assert [record.record_id for record in ordered] == ["z-1", "z-2", "a-1"]
    groups = group_records(records)
    assert len(groups) == 1
    assert groups[0].kind is GroupKind.BETWEEN_TURNS


def test_mixed_stream_grouping_preserves_transcript_turns() -> None:
    transcript_step = make_record(
        "step",
        source_epoch="transcript",
        raw_index=1,
        turn_id="turn-1",
        step_id="step-1",
        call_id="call-1",
    )
    transcript_turn = make_record(
        "turn",
        source_epoch="transcript",
        raw_index=2,
        turn_id="turn-1",
    )
    theater = make_record("theater", source_epoch="theater", raw_index=1)
    groups = group_records((transcript_step, transcript_turn, theater))
    assert [group.kind for group in groups] == [GroupKind.TURN, GroupKind.BETWEEN_TURNS]
    assert groups[0].children[0].record_ids == ("step",)
    assert groups[1].record_ids == ("theater",)


def test_grouping_chunks_turns_larger_than_the_wire_group_bounds() -> None:
    direct = tuple(
        make_record(f"direct-{index}", raw_index=index, turn_id="large-turn")
        for index in range(TRAJECTORY_MAX_GROUP_RECORD_IDS * 2 + 1)
    )
    direct_groups = group_records(direct)

    assert [len(group.record_ids) for group in direct_groups] == [
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        1,
    ]
    assert tuple(record_id for group in direct_groups for record_id in group.record_ids) == tuple(
        record.record_id for record in direct
    )
    assert len({group.group_id for group in direct_groups}) == len(direct_groups)

    stepped = tuple(
        make_record(
            f"step-{index}",
            raw_index=index,
            turn_id="large-turn",
            step_id="large-step",
        )
        for index in range(TRAJECTORY_MAX_GROUP_RECORD_IDS * 2 + 1)
    )
    step_groups = tuple(child for group in group_records(stepped) for child in group.children)

    assert [len(group.record_ids) for group in step_groups] == [
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        1,
    ]
    assert tuple(record_id for group in step_groups for record_id in group.record_ids) == tuple(
        record.record_id for record in stepped
    )
    assert len({group.group_id for group in step_groups}) == len(step_groups)

    at_limit = direct[:TRAJECTORY_MAX_GROUP_RECORD_IDS]
    assert len(group_records(at_limit)) == 1


def test_grouping_chunks_turn_records_in_source_order() -> None:
    call = make_record(
        "call",
        raw_index=0,
        turn_id="large-turn",
        call_id="call-1",
    )
    linked = make_record("linked", raw_index=1, parent_call_id="call-1")
    bulk = tuple(
        make_record(f"direct-{index}", raw_index=index + 2, turn_id="large-turn")
        for index in range(TRAJECTORY_MAX_GROUP_RECORD_IDS)
    )
    records = (call, linked, *bulk)

    groups = group_records(records)

    assert tuple(record_id for group in groups for record_id in group.record_ids) == tuple(
        record.record_id for record in records
    )


def test_grouping_chunks_mixed_turn_units_at_each_bound() -> None:
    records = tuple(
        record
        for index in range(TRAJECTORY_MAX_GROUP_RECORD_IDS + 1)
        for record in (
            make_record(f"direct-{index}", raw_index=index * 2, turn_id="large-turn"),
            make_record(
                f"step-{index}",
                raw_index=index * 2 + 1,
                turn_id="large-turn",
                step_id=f"step-{index}",
            ),
        )
    )

    groups = group_records(records)

    assert [(len(group.record_ids), len(group.children)) for group in groups] == [
        (TRAJECTORY_MAX_GROUP_RECORD_IDS, TRAJECTORY_MAX_GROUP_CHILDREN),
        (1, 1),
    ]


def test_grouping_chunks_turns_with_too_many_step_groups() -> None:
    records = tuple(
        make_record(
            f"record-{index}",
            raw_index=index,
            turn_id="large-turn",
            step_id=f"step-{index}",
        )
        for index in range(TRAJECTORY_MAX_GROUP_CHILDREN + 1)
    )

    groups = group_records(records)

    assert [len(group.children) for group in groups] == [TRAJECTORY_MAX_GROUP_CHILDREN, 1]
    assert len({group.group_id for group in groups}) == len(groups)


def test_grouping_chunks_large_between_turn_buckets() -> None:
    records = tuple(
        make_record(f"between-{index}", raw_index=index)
        for index in range(TRAJECTORY_MAX_GROUP_RECORD_IDS * 2 + 1)
    )

    groups = group_records(records)

    assert [len(group.record_ids) for group in groups] == [
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        1,
    ]
    assert tuple(record_id for group in groups for record_id in group.record_ids) == tuple(
        record.record_id for record in records
    )
    assert len({group.group_id for group in groups}) == len(groups)
    assert len(group_records(records[:TRAJECTORY_MAX_GROUP_RECORD_IDS])) == 1


def test_exact_call_link_positions_cross_stream_record() -> None:
    transcript = make_record(
        "call",
        source_epoch="transcript",
        raw_index=1,
        turn_id="turn-1",
        step_id="step-1",
        call_id="call-1",
    )
    theater = make_record(
        "result",
        source_epoch="theater",
        raw_index=1,
        parent_call_id="call-1",
    )
    groups = group_records((transcript, theater))
    assert len(groups) == 1
    assert groups[0].children[0].record_ids == ("call", "result")


def test_reliable_timestamps_place_theater_only_between_turns() -> None:
    first = make_record(
        "first",
        source_epoch="transcript",
        raw_index=1,
        turn_id="turn-1",
        timing=Timing(0, 1, provenance=TimingProvenance.SOURCE),
    )
    second = make_record(
        "second",
        source_epoch="transcript",
        raw_index=2,
        turn_id="turn-2",
        timing=Timing(3, 4, provenance=TimingProvenance.SOURCE),
    )
    theater = make_record(
        "between",
        source_epoch="theater",
        raw_index=1,
        timing=Timing(2, 2.5, provenance=TimingProvenance.SOURCE),
    )
    groups = group_records((first, second, theater))
    assert [group.kind for group in groups] == [
        GroupKind.TURN,
        GroupKind.BETWEEN_TURNS,
        GroupKind.TURN,
    ]
    assert groups[1].record_ids == ("between",)


def test_unpositioned_bus_records_are_ordered_by_bus_id() -> None:
    groups = group_records(
        (
            make_record("bus:10", source_epoch="theater", raw_index=1),
            make_record("bus:2", source_epoch="theater", raw_index=2),
        )
    )
    assert groups[0].record_ids == ("bus:2", "bus:10")


def test_between_group_ids_survive_prepend_and_regrouping() -> None:
    first = make_record(
        "first",
        source_epoch="transcript",
        raw_index=1,
        turn_id="turn-1",
        timing=Timing(0, 1, provenance=TimingProvenance.SOURCE),
    )
    second = make_record(
        "second",
        source_epoch="transcript",
        raw_index=2,
        turn_id="turn-2",
        timing=Timing(3, 4, provenance=TimingProvenance.SOURCE),
    )
    between = make_record(
        "between",
        source_epoch="theater",
        raw_index=1,
        timing=Timing(2, 2.5, provenance=TimingProvenance.SOURCE),
    )
    initial = group_records((first, second, between))
    prepended = group_records(
        (make_record("bus:1", source_epoch="theater"), first, second, between)
    )
    initial_id = next(group.group_id for group in initial if group.record_ids == ("between",))
    prepended_id = next(group.group_id for group in prepended if group.record_ids == ("between",))
    assert initial_id == prepended_id
    assert any(group.group_id == "between-turns:unpositioned" for group in prepended)
