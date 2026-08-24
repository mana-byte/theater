"""Focused causal-link domain tests."""

from __future__ import annotations

import pytest

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.trajectory.causality import CausalIndex, CausalResolution
from theater.trajectory.enums import LinkDirection, TrajectoryKind, TrajectoryLane, TrajectoryStatus
from theater.trajectory.records import ParticipantLink, TrajectoryRecord


def _record(
    participant_id: str,
    record_id: str,
    *,
    raw_index: int = 0,
    links: tuple[ParticipantLink, ...] = (),
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=0,
        participant_id=participant_id,
        source_epoch="theater-bus",
        lane=TrajectoryLane.THEATER,
        kind=TrajectoryKind.THEATER,
        source="theater",
        summary="coordination",
        status=TrajectoryStatus.COMPLETED,
        raw_index=raw_index,
        source_offset=raw_index,
        links=links,
    )


def test_participant_link_old_and_new_wire_forms_round_trip() -> None:
    old = {"participant_id": "child", "relation": "recipient", "direction": "outgoing"}
    assert ParticipantLink.from_wire(old).to_wire() == old

    link = ParticipantLink(
        "child",
        "recipient",
        LinkDirection.OUTGOING,
        target_record_id="bus:17",
        correlation_type="job_handle",
        correlation_key="child#3",
    )
    assert ParticipantLink.from_wire(link.to_wire()) == link


@pytest.mark.parametrize(
    "kwargs",
    (
        {"target_record_id": "x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1)},
        {"correlation_type": "job_handle"},
        {"correlation_key": "child#3"},
        {"correlation_type": "timestamp", "correlation_key": "123"},
        {
            "correlation_type": "job_handle",
            "correlation_key": "x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1),
        },
    ),
)
def test_participant_link_rejects_invalid_or_oversized_causality_fields(
    kwargs: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        ParticipantLink.from_wire(
            {
                "participant_id": "child",
                "relation": "recipient",
                **kwargs,
            }
        )


def test_index_distinguishes_exact_participant_only_and_unresolved_links() -> None:
    target = _record("child", "bus:7", raw_index=7)
    source = _record(
        "parent",
        "bus:7",
        raw_index=7,
        links=(
            ParticipantLink("child", "exact", target_record_id="bus:7"),
            ParticipantLink("child", "participant"),
            ParticipantLink("missing", "unresolved", target_record_id="bus:99"),
        ),
    )
    resolutions = CausalIndex((source, target)).links_for(source)

    assert [item.resolution for item in resolutions] == [
        CausalResolution.EXACT,
        CausalResolution.PARTICIPANT,
        CausalResolution.UNRESOLVED,
    ]
    assert resolutions[0].target == target
    assert resolutions[1].target is None
    assert resolutions[2].target is None


def test_index_returns_exact_counterpart_and_stable_handle_chain_in_bus_order() -> None:
    parent_send = _record(
        "parent",
        "bus:12",
        raw_index=12,
        links=(
            ParticipantLink(
                "child",
                "recipient",
                target_record_id="bus:12",
                correlation_type="job_handle",
                correlation_key="child#2",
            ),
        ),
    )
    child_receive = _record(
        "child",
        "bus:12",
        raw_index=12,
        links=(
            ParticipantLink(
                "parent",
                "sender",
                target_record_id="bus:12",
                correlation_type="job_handle",
                correlation_key="child#2",
            ),
        ),
    )
    await_end = _record(
        "parent",
        "bus:20",
        raw_index=20,
        links=(
            ParticipantLink(
                "child",
                "recipient",
                target_record_id="bus:20",
                correlation_type="job_handle",
                correlation_key="child#2",
            ),
        ),
    )
    reply = _record(
        "child",
        "bus:30",
        raw_index=30,
        links=(
            ParticipantLink(
                "parent",
                "recipient",
                target_record_id="bus:30",
                correlation_type="job_handle",
                correlation_key="child#2",
            ),
        ),
    )
    index = CausalIndex((reply, child_receive, await_end, parent_send))

    assert index.resolve(parent_send.links[0]).target == child_receive
    assert [
        (record.participant_id, record.record_id) for record in index.related_records(parent_send)
    ] == [
        ("child", "bus:12"),
        ("parent", "bus:20"),
        ("child", "bus:30"),
    ]
