"""Complete-frame trajectory response bounds."""

from __future__ import annotations

from theater import protocol
from theater.constants.trajectory import (
    TRAJECTORY_MAX_COVERAGE_GAPS,
    TRAJECTORY_RESPONSE_MAX_BYTES,
    TRAJECTORY_RESPONSE_SIZING_REQUEST_ID,
)
from theater.daemon.trajectory.cache import CacheStream, RecordChange, RecordRing
from theater.daemon.trajectory.responses import (
    empty_delta,
    fit_delta,
    fit_page,
    resync_delta,
    wire_bytes,
)
from theater.daemon.trajectory.runtime import TrajectoryStream
from theater.models import Participant
from theater.trajectory import (
    CoverageGap,
    PanelState,
    PanelStateInfo,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)


def _stream(*, gaps: list[CoverageGap] | None = None) -> TrajectoryStream:
    return TrajectoryStream(
        Participant(id="participant", harness="test"),
        CacheStream("participant", "stream", RecordRing(), 0.0),
        PanelStateInfo(PanelState.READY),
        gaps=gaps or [],
    )


def _record(index: int, summary: str) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=f"record-{index}",
        revision=0,
        participant_id="participant",
        source_epoch="epoch",
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.ASSISTANT,
        source="test",
        summary=summary,
        status=TrajectoryStatus.COMPLETED,
    )


def _page(stream: TrajectoryStream, records: tuple[TrajectoryRecord, ...]):
    return fit_page(
        stream,
        records,
        daemon_epoch="e" * 32,
        has_older=False,
        source_before=None,
        bus_before=None,
        make_older=lambda *_: "o1-" + "1" * 32,
    )


def _frame(value: dict[str, object]) -> bytes:
    return protocol.ok(TRAJECTORY_RESPONSE_SIZING_REQUEST_ID, value)


def test_page_boundary_measures_the_complete_ndjson_frame() -> None:
    records = (
        *(_record(index, "x" * 16_384) for index in range(62)),
        _record(62, "x" * 8_532),
    )

    page = _page(_stream(), records)

    assert len(page.records) < len(records)
    assert len(_frame(page.to_wire())) <= TRAJECTORY_RESPONSE_MAX_BYTES


def test_page_trims_the_record_that_crosses_the_complete_frame_cap() -> None:
    records = (
        *(_record(index, "x" * 16_384) for index in range(62)),
        _record(62, "x" * 8_532),
        _record(63, "x"),
    )

    page = _page(_stream(), records)

    assert len(page.records) == len(records) - 1
    assert len(_frame(page.to_wire())) <= TRAJECTORY_RESPONSE_MAX_BYTES


def test_follow_delta_complete_frame_stays_within_the_cap() -> None:
    stream = _stream()
    changes = tuple(RecordChange(index + 1, _record(index, "x" * 16_384)) for index in range(100))

    delta = fit_delta(stream, changes, daemon_epoch="e" * 32, after_sequence=0)

    assert delta is not None
    assert len(delta.upserts) < len(changes)
    assert len(_frame(delta.to_wire())) <= TRAJECTORY_RESPONSE_MAX_BYTES


def test_empty_and_state_only_deltas_stay_within_the_cap() -> None:
    stream = _stream()
    empty = empty_delta(stream, daemon_epoch="e" * 32, sequence=0)
    resync = resync_delta("stream", "request a fresh snapshot")

    assert len(_frame(empty.to_wire())) <= TRAJECTORY_RESPONSE_MAX_BYTES
    assert len(_frame(resync.to_wire())) <= TRAJECTORY_RESPONSE_MAX_BYTES


def test_empty_page_bounds_large_coverage_gaps_deterministically() -> None:
    gaps = [
        CoverageGap("s" * 256, "g" * 16_384, start=f"start-{index}", end=f"end-{index}")
        for index in range(TRAJECTORY_MAX_COVERAGE_GAPS)
    ]

    first = _page(_stream(gaps=gaps), ())
    second = _page(_stream(gaps=gaps), ())

    assert len(_frame(first.to_wire())) <= TRAJECTORY_RESPONSE_MAX_BYTES
    assert first.coverage == second.coverage
    assert first.coverage.gaps[0].stream == "coverage"
    retained = first.coverage.gaps[1:]
    assert retained == tuple(gaps[-len(retained) :])


def test_records_take_priority_over_large_coverage_gaps() -> None:
    records = tuple(_record(index, "x" * 16_384) for index in range(70))
    gaps = [
        CoverageGap("s" * 256, "g" * 16_384, start=f"start-{index}", end=f"end-{index}")
        for index in range(TRAJECTORY_MAX_COVERAGE_GAPS)
    ]

    without_gaps = _page(_stream(), records)
    with_gaps = _page(_stream(gaps=gaps), records)

    assert with_gaps.records == without_gaps.records
    assert with_gaps.coverage.gaps[0].stream == "coverage"
    assert len(_frame(with_gaps.to_wire())) <= TRAJECTORY_RESPONSE_MAX_BYTES


def test_multibyte_response_content_is_sized_as_encoded_bytes() -> None:
    ascii_page = _page(_stream(), (_record(0, "x" * 100),))
    multibyte_page = _page(_stream(), (_record(0, "é" * 100),))

    assert wire_bytes(multibyte_page.to_wire()) == len(_frame(multibyte_page.to_wire()))
    assert wire_bytes(multibyte_page.to_wire()) > wire_bytes(ascii_page.to_wire())


def test_page_accepts_a_transcript_cursor_larger_than_an_identifier() -> None:
    stream = _stream()
    stream.transcript_floor = "source-cursor-" + "x" * 600

    page = _page(stream, (_record(0, "record"),))

    assert page.coverage.transcript_floor == stream.transcript_floor
    assert len(_frame(page.to_wire())) <= TRAJECTORY_RESPONSE_MAX_BYTES
