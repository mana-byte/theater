"""Trajectory bounds: previews, detail caps and complete response frames."""

from __future__ import annotations

import re

import pytest

from tests.support.trajectory_records import make_record
from theater import protocol
from theater.constants.trajectory import (
    TRAJECTORY_DETAIL_NAME_MAX_BYTES,
    TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
    TRAJECTORY_MAX_COVERAGE_GAPS,
    TRAJECTORY_MAX_DETAILS_PER_RECORD,
    TRAJECTORY_RESPONSE_MAX_BYTES,
    TRAJECTORY_RESPONSE_SIZING_REQUEST_ID,
)
from theater.daemon.trajectory import responses as responses_module
from theater.daemon.trajectory.cache import CacheStream, RecordChange, RecordRing
from theater.daemon.trajectory.project import event_to_fact, fact_to_record
from theater.daemon.trajectory.responses import (
    empty_delta,
    fit_delta,
    fit_page,
    resync_delta,
    wire_bytes,
)
from theater.daemon.trajectory.stream import TrajectoryStream
from theater.harness.contracts.events import Event, EventKind
from theater.models import Participant
from theater.trajectory import (
    ContentFormat,
    ContentPreview,
    CoverageGap,
    DetailField,
    PanelState,
    PanelStateInfo,
    TrajectoryCoverage,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryValidationError,
    bounded_preview,
)
from theater.trajectory.content import _rebound_preview


def test_utf8_preview_keeps_safe_head_tail_and_exact_omission() -> None:
    original = "é" * 20_000
    preview = bounded_preview(original)
    marker = f"… {preview.omitted_bytes} bytes omitted …"
    shown = preview.text.replace(marker, "", 1)
    assert len(preview.text.encode("utf-8")) <= 16 * 1024
    assert preview.omitted_bytes == len(original.encode("utf-8")) - len(shown.encode("utf-8"))
    assert shown.startswith("é") and shown.endswith("é")
    assert marker in preview.text

    unsafe = bounded_preview("\x1b[31m[bold]\x00")
    assert "\x1b" not in unsafe.text
    assert "\x00" not in unsafe.text
    assert unsafe.text == r"\x1b[31m[bold]\x00"
    assert ContentPreview(r"[bold]\literal").text == r"[bold]\literal"


def test_record_detail_fields_obey_field_and_aggregate_byte_caps() -> None:
    details = tuple(
        DetailField.from_text(str(index), "x" * (20 * 1024), format=ContentFormat.TEXT)
        for index in range(3)
    )
    record = make_record("r", details=details)
    assert all(detail.preview.encoded_bytes <= 16 * 1024 for detail in record.details)
    assert sum(detail.preview.encoded_bytes for detail in record.details) <= 32 * 1024
    assert any(detail.preview.omitted_bytes for detail in record.details)


@pytest.mark.parametrize("unit", ["abcdefgh ", "é😊\x1b\u0080"])
def test_large_preview_retains_bounded_source_with_exact_reclipping(unit: str) -> None:
    original = unit * 100_000
    preview = ContentPreview.from_text(original)
    for budget in (16_384, 4096, 100, 1):
        preview = _rebound_preview(preview, max_bytes=budget)
        assert preview._source is not None
        head, tail = preview._source
        retained = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
        assert retained <= budget
        assert preview.encoded_bytes <= budget
        assert preview.omitted_bytes == len(original.encode("utf-8")) - retained
        assert original.startswith(head) and original.endswith(tail)
    with pytest.raises(TrajectoryValidationError):
        ContentPreview.from_text(unit * 100_000 + "\ud800" + unit * 100_000)


def test_detail_rebounding_keeps_one_accurate_omission_marker() -> None:
    marker_pattern = re.compile(r"… (\d+) bytes omitted …")
    record = make_record(
        "bounded",
        details=(
            DetailField.from_text("first", "\x00" * 12_000 + "a" * 10_000),
            DetailField.from_text("second", "\x1b" * 12_000 + "b" * 10_000),
            DetailField.from_text("third", "c" * 20_000),
        ),
    )
    assert len(record.details) == 2
    for detail in record.details:
        matches = marker_pattern.findall(detail.preview.text)
        assert len(matches) == 1
        assert int(matches[0]) == detail.preview.omitted_bytes


def test_adversarial_bounds_cover_names_details_groups_and_gaps() -> None:
    with pytest.raises(TrajectoryValidationError):
        DetailField.from_text("x" * (TRAJECTORY_DETAIL_NAME_MAX_BYTES + 1), "value")
    with pytest.raises(TrajectoryValidationError):
        make_record("x" * 513)
    details = tuple(
        DetailField.from_text(str(index), "value")
        for index in range(TRAJECTORY_MAX_DETAILS_PER_RECORD + 10)
    )
    record = make_record("bounded", details=details)
    assert len(record.details) <= TRAJECTORY_MAX_DETAILS_PER_RECORD
    assert (
        sum(
            len(detail.name.encode("utf-8")) + detail.preview.encoded_bytes
            for detail in record.details
        )
        <= TRAJECTORY_DETAIL_RECORD_MAX_BYTES
    )
    assert all(detail.preview.text for detail in record.details)
    with pytest.raises(TrajectoryValidationError):
        TrajectoryCoverage(
            gaps=tuple(
                CoverageGap("stream", "gap") for _ in range(TRAJECTORY_MAX_COVERAGE_GAPS + 1)
            )
        )


def test_raw_text_preserves_literal_markup_and_is_bounded() -> None:
    raw = "[bold]\\literal " + "x" * (20 * 1024)
    fact = event_to_fact(
        Event(kind=EventKind.ASSISTANT, text="clipped", raw_text=raw),
    )
    assert fact.details[0].name == "raw"
    assert fact.details[0].preview.text.startswith("[bold]\\literal")
    assert fact.details[0].preview.encoded_bytes <= 16 * 1024
    record = fact_to_record(fact, participant_id="p", source_epoch="epoch")
    assert record.details[0].preview.text == fact.details[0].preview.text
    assert len(record.summary.encode("utf-8")) <= 16 * 1024


def test_control_escaping_does_not_inflate_omitted_source_bytes() -> None:
    original = "\x1b" + "x" * 20_000
    preview = bounded_preview(original)
    marker = f"… {preview.omitted_bytes} bytes omitted …"
    assert marker in preview.text
    displayed_without_marker = preview.text.replace(marker, "", 1)
    assert preview.omitted_bytes == len(original.encode("utf-8")) - (
        len(displayed_without_marker.encode("utf-8")) - 3
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
        _record(62, "x" * 8_186),
    )

    page = _page(_stream(), records)

    assert len(page.records) == len(records)
    assert len(_frame(page.to_wire())) == TRAJECTORY_RESPONSE_MAX_BYTES


def test_page_trims_the_record_that_crosses_the_complete_frame_cap() -> None:
    records = (
        *(_record(index, "x" * 16_384) for index in range(62)),
        _record(62, "x" * 8_186),
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


def test_fit_helpers_project_response_values_once_per_attempt(monkeypatch) -> None:
    original = responses_module.response_values_for
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(responses_module, "response_values_for", counted)
    _page(_stream(), tuple(_record(index, "x" * 16_384) for index in range(100)))
    changes = tuple(RecordChange(index + 1, _record(index, "x" * 16_384)) for index in range(100))
    fit_delta(_stream(), changes, daemon_epoch="e" * 32, after_sequence=0)

    assert calls == 2


def test_empty_and_state_only_deltas_stay_within_the_cap() -> None:
    stream = _stream()
    empty = empty_delta(stream, daemon_epoch="e" * 32, sequence=0)
    resync = resync_delta("stream", "request a fresh snapshot")

    assert len(_frame(empty.to_wire())) <= TRAJECTORY_RESPONSE_MAX_BYTES
    assert len(_frame(resync.to_wire())) <= TRAJECTORY_RESPONSE_MAX_BYTES


def test_overview_marks_evicted_daemon_cache_scope_incomplete() -> None:
    stream = _stream()
    stream.ring.max_bytes = 1
    stream.ring.merge((_record(0, "record"),))

    delta = empty_delta(stream, daemon_epoch="e" * 32, sequence=0)

    assert delta.overview is not None
    assert not delta.overview.scope_complete
    assert [reason.value for reason in delta.overview.incomplete_reasons] == ["cache_evicted"]


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
