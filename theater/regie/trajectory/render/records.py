"""Pure bounded render helpers shared by trajectory widgets."""

from __future__ import annotations

from rich.cells import cell_len, set_cell_size
from rich.text import Text

from theater.constants.regie_trajectory import (
    KIND_GLYPHS_BY_VALUE,
    LANE_GLYPHS_BY_VALUE,
    STYLE_DURATION,
    STYLE_MATCHED,
    TOOLTIP_DELAY,
    TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD,
)
from theater.constants.trajectory import TRAJECTORY_TOOLTIP_SUMMARY_MAX_CELLS
from theater.formatting import event_stamp
from theater.regie.trajectory.render.formatting import (
    format_duration,
    plain_text,
    sanitize_text,
    status_label,
)
from theater.regie.trajectory.render.formatting import (
    format_milliseconds as _format_milliseconds,
)
from theater.trajectory import (
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
)

LANE_GLYPHS = {TrajectoryLane(value): glyph for value, glyph in LANE_GLYPHS_BY_VALUE.items()}
KIND_GLYPHS = {TrajectoryKind(value): glyph for value, glyph in KIND_GLYPHS_BY_VALUE.items()}


def bottom_aligned_cell(value: Text | str, height: int) -> Text:
    """Place one-line table content on the last line of a fixed-height row."""
    aligned = Text("\n" * max(0, height - 1))
    if isinstance(value, Text):
        aligned.append_text(value)
        aligned.justify = value.justify
    else:
        aligned.append(value)
    aligned.no_wrap = True
    aligned.overflow = "ellipsis"
    return aligned


def _compact(value: str, limit: int) -> str:
    return sanitize_text(value).replace("\r", " ").replace("\n", " ")[:limit]


def lane_glyph(lane: TrajectoryLane) -> str:
    return LANE_GLYPHS.get(lane, "?")


def kind_glyph(kind: TrajectoryKind) -> str:
    return KIND_GLYPHS.get(kind, "?")


def compact_number(value: int) -> str:
    """Render a non-negative count using the overview's compact notation."""
    if value < TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD:
        return str(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            return f"{value / divisor:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def compact_cost(value: float) -> str:
    """Render a reported dollar value using the overview's compact precision."""
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def supports_duration_interval(record: TrajectoryRecord) -> bool:
    """Whether a record has independently reported usable interval data."""
    timing = record.timing
    return (
        timing is not None
        and timing.provenance
        in {
            TimingProvenance.SOURCE,
            TimingProvenance.OBSERVED,
        }
        and (
            timing.duration_ms is not None or (timing.start is not None and timing.end is not None)
        )
    )


def record_line(
    record: TrajectoryRecord,
    index: int,
    *,
    selected: bool = False,
    hovered: bool = False,
    duration_mode: bool = False,
    depth: int = 0,
) -> Text:
    """Render one compact, non-wrapping ledger line."""
    marker = "▶" if selected else ("·" if hovered else " ")
    duration = format_duration(record.timing)
    position = f"{index:>4}" if not duration_mode else f"dur {duration:>7}"
    indent = "  " * depth
    line = Text(no_wrap=True, overflow="crop")
    line.append(f"{indent}{marker} {position} ")
    line.append(f"{kind_glyph(record.kind)} ")
    line.append(f"{_compact(record.source, 16):<16} ")
    line.append(f"{_compact(record.summary, 64)} ")
    line.append(f"{status_label(record.status):<11} ")
    line.append(duration, style=STYLE_DURATION if duration_mode else STYLE_MATCHED)
    return line


def tooltip_text(
    record: TrajectoryRecord,
    *,
    timing: Timing | None = None,
    timing_scope: str | None = None,
) -> str:
    """Return bounded, type-aware hover detail."""
    usage = record.usage
    identity = record.source
    if usage is not None and usage.model and usage.model != identity:
        identity = f"{identity} · {usage.model}"
    elif record.kind is TrajectoryKind.TOOL_CALL and record.summary:
        identity = f"{identity} · {' '.join(plain_text(record.summary).splitlines())}"
    kind = record.kind.value.replace("_", " ").upper()
    heading = f"{kind_glyph(record.kind)} {kind} · {identity} · {status_label(record.status)}"
    summary = " ".join(plain_text(record.summary).splitlines()) or "No preview available"
    metrics: list[str] = []
    timing = timing or record.timing
    duration = format_duration(timing)
    if timing is not None and timing.duration_ms is not None:
        provenance = _timing_provenance_suffix(timing)
        metrics.append(f"{timing_scope or 'total'} {duration}{provenance}")
    elif timing is not None and (timing.end is not None or timing.start is not None):
        timestamp = timing.end if timing.end is not None else timing.start
        assert timestamp is not None
        qualifier = (
            "observed"
            if timing.provenance is TimingProvenance.OBSERVED
            else timing.provenance.value
        )
        phase = "ended" if timing.end is not None and timing_scope is not None else "at"
        scope = f"{timing_scope} " if timing_scope else ""
        metrics.append(f"{scope}{phase} {event_stamp(timestamp)} · {qualifier}")
    elif record.kind in _POINT_EVENT_KINDS:
        metrics.append("point event")
    else:
        metrics.append(f"{timing_scope + ' ' if timing_scope else ''}duration unavailable")
    if timing is not None and timing.ttft_ms is not None:
        metrics.append(f"TTFT {_format_milliseconds(timing.ttft_ms)}")
    if timing is not None and timing.generation_duration_ms is not None:
        metrics.append(f"generation {_format_milliseconds(timing.generation_duration_ms)}")
    if usage is not None:
        metrics.extend(
            (
                f"in {compact_number(usage.input_tokens)}",
                f"out {compact_number(usage.output_tokens)}",
            )
        )
        if usage.cost_usd is not None:
            metrics.append(f"cost ${compact_cost(usage.cost_usd)}")
    detail = " · ".join(metrics)
    return "\n".join(_bounded_tooltip_line(line) for line in (heading, summary, detail))


_POINT_EVENT_KINDS = frozenset(
    {
        TrajectoryKind.USER,
        TrajectoryKind.SYSTEM,
        TrajectoryKind.CONTEXT,
        TrajectoryKind.THEATER,
        TrajectoryKind.SPAWN,
        TrajectoryKind.RESUME,
        TrajectoryKind.SEND,
        TrajectoryKind.RECEIVE,
        TrajectoryKind.KILL,
        TrajectoryKind.TRANSCRIPT_BOUNDARY,
        TrajectoryKind.SESSION_BOUNDARY,
        TrajectoryKind.OBSERVATION_ERROR,
    }
)


def _timing_provenance_suffix(timing: Timing) -> str:
    if timing.provenance is TimingProvenance.SOURCE:
        return ""
    return f" {timing.provenance.value}"


def _bounded_tooltip_line(value: str) -> str:
    value = sanitize_text(value).replace("\r", " ").replace("\n", " ")
    if cell_len(value) <= TRAJECTORY_TOOLTIP_SUMMARY_MAX_CELLS:
        return value
    return set_cell_size(value, TRAJECTORY_TOOLTIP_SUMMARY_MAX_CELLS - 1).rstrip() + "…"


def count_label(value: str, count: int) -> str:
    return f"{value} ({count})"


def details_size(record: TrajectoryRecord) -> int:
    return sum(
        len(field.name.encode("utf-8")) + field.preview.encoded_bytes for field in record.details
    )


__all__ = [
    "KIND_GLYPHS",
    "LANE_GLYPHS",
    "TOOLTIP_DELAY",
    "compact_cost",
    "compact_number",
    "count_label",
    "details_size",
    "format_duration",
    "kind_glyph",
    "lane_glyph",
    "plain_text",
    "record_line",
    "sanitize_text",
    "status_label",
    "tooltip_text",
]
