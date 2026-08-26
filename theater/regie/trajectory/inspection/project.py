"""Plain bounded detail projection for trajectory records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from theater.constants.trajectory import TRAJECTORY_DETAIL_RECORD_MAX_BYTES
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.inspection.lines import (
    InspectorLine,
    failure_lines,
    request_association_lines,
    request_summary_lines,
    request_timing_lines,
    request_usage_lines,
    retry_lines,
)
from theater.regie.trajectory.render.formatting import (
    format_duration,
    plain_text,
    status_label,
)
from theater.trajectory import (
    ContentFormat,
    DetailField,
    ParticipantLink,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryRequest,
    bounded_preview,
)


@dataclass(frozen=True, slots=True)
class RecordDetailProjection:
    tab: InspectorTab
    tabs: tuple[InspectorTab, ...]
    copy_text: str
    participant_links: Mapping[int, ParticipantLink]
    record_links: Mapping[int, str]


def tabs_for_record(record: TrajectoryRecord | None) -> tuple[InspectorTab, ...]:
    if record is None:
        return (InspectorTab.SUMMARY,)
    if record.kind in {TrajectoryKind.SYSTEM, TrajectoryKind.CONTEXT}:
        return (InspectorTab.CURRENT, InspectorTab.PREVIOUS, InspectorTab.DIFF)
    if (
        record.kind in {TrajectoryKind.ASSISTANT, TrajectoryKind.REASONING}
        or record.lane is TrajectoryLane.MODEL
    ):
        return (
            InspectorTab.SUMMARY,
            InspectorTab.OUTPUT,
            InspectorTab.REASONING,
            InspectorTab.USAGE,
            InspectorTab.TIMING,
            InspectorTab.ASSOCIATIONS,
        )
    if (
        record.kind in {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}
        or record.lane is TrajectoryLane.TOOLS
    ):
        return (InspectorTab.SUMMARY, InspectorTab.INPUT, InspectorTab.RESULT, InspectorTab.TIMING)
    if record.kind in {TrajectoryKind.THEATER_CALL, TrajectoryKind.THEATER_RESULT}:
        return (InspectorTab.SUMMARY, InspectorTab.INPUT, InspectorTab.RESULT, InspectorTab.TIMING)
    if record.kind is TrajectoryKind.USER or record.lane is TrajectoryLane.INPUT:
        return (InspectorTab.PREVIEW, InspectorTab.RAW, InspectorTab.SOURCE)
    if record.lane is TrajectoryLane.THEATER:
        return (InspectorTab.SUMMARY, InspectorTab.PAYLOAD, InspectorTab.TIMING)
    return (InspectorTab.SUMMARY, InspectorTab.TIMING)


_TAB_FIELD_ALIASES: dict[InspectorTab, frozenset[str]] = {
    InspectorTab.OUTPUT: frozenset({"assistant_output", "content", "output", "response", "text"}),
    InspectorTab.REASONING: frozenset({"reasoning", "reasoning_content", "reasoning_summary"}),
    InspectorTab.INPUT: frozenset({"args", "arguments", "input", "parameters", "tool_input"}),
    InspectorTab.RESULT: frozenset({"output", "response", "result", "tool_result"}),
    InspectorTab.PREVIEW: frozenset({"content", "input", "preview", "prompt", "text"}),
    InspectorTab.RAW: frozenset({"raw", "raw_text", "source_text", "transcript"}),
    InspectorTab.PAYLOAD: frozenset({"data", "event", "message", "payload"}),
    InspectorTab.CURRENT: frozenset({"current", "context_current", "current_context", "state"}),
    InspectorTab.PREVIOUS: frozenset(
        {"previous", "context_previous", "previous_context", "previous_state"}
    ),
    InspectorTab.DIFF: frozenset({"context_diff", "diff", "changes"}),
}


def _field_key(name: str) -> str:
    return name.casefold().replace("-", "_").replace(" ", "_")


def _fields_for_tab(record: TrajectoryRecord, tab: InspectorTab) -> tuple[DetailField, ...]:
    aliases = _TAB_FIELD_ALIASES.get(tab, frozenset())
    return tuple(field for field in record.details if _field_key(field.name) in aliases)


def _format_detail(field: DetailField) -> str:
    preview = field.preview
    if field.format in {ContentFormat.IMAGE, ContentFormat.BINARY}:
        total = preview.encoded_bytes + preview.omitted_bytes
        return f"{field.format.value} metadata ({total} bytes)"
    if field.format is ContentFormat.JSON:
        try:
            return json.dumps(
                json.loads(preview.text), ensure_ascii=False, indent=2, sort_keys=True
            )
        except (TypeError, ValueError):
            return plain_text(preview.text)
    return plain_text(preview.text)


def _participant_line(link: ParticipantLink) -> str:
    target = (
        f" · exact target {link.target_record_id}"
        if link.target_record_id is not None
        else " · participant only"
    )
    return f"participant {link.direction.value}: {link.participant_id} ({link.relation}){target}"


def _field_lines(fields: tuple[DetailField, ...]) -> tuple[InspectorLine, ...]:
    lines: list[InspectorLine] = []
    for field in fields:
        values = _format_detail(field).split("\n")
        lines.append(InspectorLine(f"{field.name}: {values[0]}"))
        lines.extend(InspectorLine(value) for value in values[1:])
    return tuple(lines)


def _record_tab_lines(  # noqa: PLR0912
    record: TrajectoryRecord,
    tab: InspectorTab,
    request: TrajectoryRequest | None,
) -> tuple[InspectorLine, ...]:
    if tab is InspectorTab.SUMMARY:
        lines = [*_field_lines(record.details[:8])]
        if request is not None:
            lines.extend(request_summary_lines(request))
        else:
            lines.extend(failure_lines(record.failure))
            lines.extend(retry_lines(record.retry_of_record_id, record.retry_attempt))
        return tuple(lines)
    if tab in {
        InspectorTab.OUTPUT,
        InspectorTab.RESULT,
        InspectorTab.PAYLOAD,
        InspectorTab.RAW,
        InspectorTab.INPUT,
        InspectorTab.REASONING,
        InspectorTab.PREVIEW,
    }:
        fields = _fields_for_tab(record, tab)
        return _field_lines(fields) if fields else (InspectorLine(f"No {tab.value} supplied."),)
    if tab in {InspectorTab.CURRENT, InspectorTab.PREVIOUS, InspectorTab.DIFF}:
        fields = _fields_for_tab(record, tab)
        return (
            _field_lines(fields)
            if fields
            else (InspectorLine(f"No {tab.value} context supplied."),)
        )
    if tab is InspectorTab.USAGE:
        if request is not None:
            return request_usage_lines(request)
        if record.usage is not None:
            return (
                InspectorLine(
                    json.dumps(record.usage.to_wire(), ensure_ascii=False, indent=2, sort_keys=True)
                ),
            )
        return (InspectorLine("No usage recorded."),)
    if tab is InspectorTab.TIMING:
        if request is not None:
            return request_timing_lines(request)
        lines = [InspectorLine(f"Duration: {format_duration(record.timing)}")]
        if record.timing is not None:
            lines.extend(
                InspectorLine(value)
                for value in (
                    f"Start: {record.timing.start}",
                    f"End: {record.timing.end}",
                    f"Provenance: {record.timing.provenance.value}",
                )
            )
        return tuple(lines)
    if tab is InspectorTab.ASSOCIATIONS:
        return request_association_lines(request)
    if tab is InspectorTab.SOURCE:
        return (
            InspectorLine(f"Source: {record.source}"),
            InspectorLine(f"Epoch: {record.source_epoch}"),
        )
    return ()


def _detail_lines(
    record: TrajectoryRecord | None,
    tab: InspectorTab,
    request: TrajectoryRequest | None = None,
) -> tuple[list[str], dict[int, ParticipantLink], dict[int, str]]:
    if record is None:
        return ["No record selected."], {}, {}
    lines = [
        f"{record.kind.value} · {record.source} · {status_label(record.status)}",
        *record.summary.split("\n"),
    ]
    record_link_lines: dict[int, str] = {}
    for value in _record_tab_lines(record, tab, request):
        lines.append(value.text)
        if value.target_record_id is not None:
            record_link_lines[len(lines) - 1] = value.target_record_id
    link_lines: dict[int, ParticipantLink] = {}
    if tab not in {InspectorTab.USAGE, InspectorTab.TIMING, InspectorTab.SOURCE}:
        for link in record.links:
            lines.append(_participant_line(link))
            link_lines[len(lines) - 1] = link
    return lines, link_lines, record_link_lines


def _bounded_lines(lines: list[str]) -> str:
    return bounded_preview(
        "\n".join(plain_text(line) for line in lines),
        max_bytes=TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
    ).text


def _project_record_details(
    record: TrajectoryRecord | None,
    requested_tab: InspectorTab,
    request: TrajectoryRequest | None = None,
    *,
    resolve_tab: bool,
) -> RecordDetailProjection:
    tabs = tabs_for_record(record)
    active = requested_tab if not resolve_tab or requested_tab in tabs else tabs[0]
    lines, participant_links, record_links = _detail_lines(record, active, request)
    copy_text = _bounded_lines(lines)
    bounded = copy_text.splitlines()
    visible_participant_links = {
        line_index: link
        for line_index, link in participant_links.items()
        if line_index < len(bounded) and bounded[line_index] == plain_text(lines[line_index])
    }
    visible_record_links = {
        line_index: record_id
        for line_index, record_id in record_links.items()
        if line_index < len(bounded) and bounded[line_index] == plain_text(lines[line_index])
    }
    return RecordDetailProjection(
        active,
        tabs,
        copy_text,
        MappingProxyType(visible_participant_links),
        MappingProxyType(visible_record_links),
    )


def project_record_details(
    record: TrajectoryRecord,
    requested_tab: InspectorTab,
    request: TrajectoryRequest | None = None,
) -> RecordDetailProjection:
    """Build bounded plain detail and links for one record tab."""
    return _project_record_details(record, requested_tab, request, resolve_tab=True)


def active_detail_tab(record: TrajectoryRecord, requested: InspectorTab) -> InspectorTab:
    return project_record_details(record, requested).tab


def detail_text(
    record: TrajectoryRecord | None,
    tab: InspectorTab,
    request: TrajectoryRequest | None = None,
) -> str:
    """Build the exact bounded text exposed by the active detail tab."""
    return _project_record_details(record, tab, request, resolve_tab=False).copy_text


def detail_link_line_ids(record: TrajectoryRecord | None, tab: InspectorTab) -> dict[int, str]:
    return {
        line_index: link.participant_id
        for line_index, link in _project_record_details(
            record, tab, resolve_tab=False
        ).participant_links.items()
    }


def detail_links_by_line(
    record: TrajectoryRecord | None,
    tab: InspectorTab,
    request: TrajectoryRequest | None = None,
) -> dict[int, ParticipantLink]:
    return dict(_project_record_details(record, tab, request, resolve_tab=False).participant_links)


def detail_record_links_by_line(
    record: TrajectoryRecord | None,
    tab: InspectorTab,
    request: TrajectoryRequest | None = None,
) -> dict[int, str]:
    return dict(_project_record_details(record, tab, request, resolve_tab=False).record_links)


__all__ = [
    "RecordDetailProjection",
    "active_detail_tab",
    "detail_link_line_ids",
    "detail_links_by_line",
    "detail_record_links_by_line",
    "detail_text",
    "project_record_details",
    "tabs_for_record",
]
