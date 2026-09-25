"""A span as one structured page: a header plus ordered, foldable sections."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from rich.console import RenderableType
from rich.style import Style
from rich.text import Text

from regie.trajectory.rich.inspection.content import (
    Palette,
    lexer_for_path,
    render_content,
    render_data,
    unwrap,
)
from regie.trajectory.rich.inspection.links import DETAIL_RECORD_TARGET_META, participant_link_meta
from regie.trajectory.rich.render.formatting import format_duration, format_milliseconds
from regie.trajectory.rich.render.records import compact_cost, compact_number
from regie.trajectory.ui_constants import (
    KIND_GLYPHS_BY_VALUE,
    TOOL_ROW_INPUT_KEY_PRIORITY,
    TOOL_ROW_INPUT_VALUE_MAX_CHARS,
)
from theater.frontend.trajectory import (
    ContentFormat,
    DetailField,
    ParticipantLink,
    Timing,
    TrajectoryFailure,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryStatus,
    TrajectoryToolOperation,
    TrajectoryUsage,
    sanitize_text,
)

RecordLookup = Callable[[str], TrajectoryRecord | None]

_INPUT = frozenset({"args", "arguments", "input", "parameters", "tool_input", "prompt"})
_RESULT = frozenset({"output", "response", "result", "tool_result"})
_OUTPUT = frozenset({"assistant_output", "content", "output", "response", "text"})
_REASONING = frozenset({"reasoning", "reasoning_content", "reasoning_summary"})
_PAYLOAD = frozenset({"data", "event", "message", "payload"})
_CURRENT = frozenset({"current", "context_current", "current_context", "state"})
_PREVIOUS = frozenset({"previous", "context_previous", "previous_context", "previous_state"})
_DIFF = frozenset({"context_diff", "diff", "changes"})
# Fields the header already says (tool name, effect) are not repeated as sections.
_HEADER_FIELDS = frozenset({"tool", "tool_name", "effect_kind", "name"})
_PATH_KEYS = ("file_path", "filePath", "path", "filename", "file")


@dataclass(frozen=True, slots=True)
class Section:
    """One titled block; `long_folds` sections collapse after a few lines.

    A callable body re-renders with the set of data branches the user toggled.
    """

    key: str
    title: str
    body: RenderableType | Callable[[frozenset[str]], RenderableType]
    copy_text: str
    folded: bool = False
    long_folds: bool = True

    @property
    def role(self) -> str:
        """What the section holds, which picks its heading colour."""
        return _ROLES.get(self.key.split(":", 1)[0], "other")

    def render(self, toggled: frozenset[str] = frozenset()) -> RenderableType:
        return self.body(toggled) if callable(self.body) else self.body


_ROLES = {
    "input": "input",
    "prompt": "input",
    "current": "input",
    "output": "output",
    "result": "output",
    "summary": "output",
    "payload": "output",
    "diff": "output",
    "reasoning": "reasoning",
    "tools": "tools",
    "error": "error",
    "participants": "links",
    "debug": "debug",
}


@dataclass(frozen=True, slots=True)
class SpanSheet:
    title: Text
    meta: Text | None
    sections: tuple[Section, ...]

    @property
    def copy_text(self) -> str:
        return "\n\n".join(f"## {section.title}\n{section.copy_text}" for section in self.sections)


def build_sheet(
    record: TrajectoryRecord,
    palette: Palette,
    *,
    tool: TrajectoryToolOperation | None = None,
    request: TrajectoryRequest | None = None,
    lookup: RecordLookup | None = None,
) -> SpanSheet:
    """Header facts and sections for one span, ordered for reading at a glance."""
    if tool is not None:
        return _tool_sheet(tool, palette)
    lookup = lookup or (lambda _record_id: None)
    model = record.lane is TrajectoryLane.MODEL
    sections = [*_record_sections(record, request, lookup, palette)]
    sections.extend(_failure_sections(record.failure, palette))
    sections.extend(_link_sections(record.links, palette))
    if not sections:
        empty = Text(f"No content reported by {record.source}.", style=palette.muted)
        sections.append(Section("empty", "Content", empty, empty.plain, long_folds=False))
    sections.append(_debug_section(_record_debug(record, request), palette))
    usage = (request.usage if request is not None else None) or record.usage
    return SpanSheet(
        _title(
            KIND_GLYPHS_BY_VALUE.get(record.kind.value, "?"),
            record.kind.value.replace("_", " ").capitalize(),
            None,
            record.status,
            (_request_timing(request) if model else None) or record.timing,
            palette,
        ),
        _usage_line(usage, request, palette) if model else None,
        tuple(sections),
    )


def _request_timing(request: TrajectoryRequest | None) -> Timing | None:
    timing = request.timing if request is not None else None
    return timing if timing is not None and timing.duration_ms is not None else None


# ---- tools ----------------------------------------------------------------------------


def _tool_sheet(tool: TrajectoryToolOperation, palette: Palette) -> SpanSheet:
    inputs = _matching(tool.call_details, _INPUT)
    results = _matching(tool.result_details, _RESULT)
    path = _input_value(inputs, _PATH_KEYS)
    sections = [
        *_field_sections("input", "Input", inputs, palette),
        *_failure_sections(tool.failure, palette),
        *_field_sections("result", "Result", results, palette, lexer=lexer_for_path(path)),
        *_other_sections((*tool.call_details, *tool.result_details), {*inputs, *results}, palette),
    ]
    debug = {
        "Call ID": tool.call_id,
        "Request": tool.request_id,
        "Identity": tool.identity.value,
        "Parent call": tool.parent_call_id,
        "Child calls": ", ".join(tool.child_call_ids) or None,
        "Retry of": tool.retry_of_record_id,
        "Source": tool.source,
    }
    sections.append(_debug_section(debug, palette))
    name = f"{tool.mcp_server} › {tool.mcp_tool}" if tool.mcp_server else tool.tool_name
    return SpanSheet(
        _title(
            "⚙",
            name or "unknown tool",
            _input_value(inputs, TOOL_ROW_INPUT_KEY_PRIORITY),
            tool.status,
            tool.timing,
            palette,
        ),
        None,
        tuple(sections),
    )


def _input_value(fields: Iterable[DetailField], keys: Iterable[str]) -> str | None:
    """The first matching top-level argument, such as a command or a path."""
    for field in fields:
        decoded = unwrap(field.preview.text)
        if isinstance(decoded, dict):
            for key in keys:
                value = decoded.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return None


# ---- records --------------------------------------------------------------------------


def _record_sections(
    record: TrajectoryRecord,
    request: TrajectoryRequest | None,
    lookup: RecordLookup,
    palette: Palette,
) -> Iterable[Section]:
    kind = record.kind
    if kind in {TrajectoryKind.SYSTEM, TrajectoryKind.CONTEXT}:
        yield from _field_sections(
            "current", "Current", _matching(record.details, _CURRENT), palette
        )
        yield from _field_sections("diff", "Diff", _matching(record.details, _DIFF), palette)
        yield from _field_sections(
            "previous", "Previous", _matching(record.details, _PREVIOUS), palette, folded=True
        )
        used = {*_CURRENT, *_DIFF, *_PREVIOUS}
    elif kind is TrajectoryKind.REASONING:
        yield from _text_sections("reasoning", "Reasoning", record, _REASONING, palette)
        used = set(_REASONING)
    elif record.lane is TrajectoryLane.MODEL:
        yield from _sibling_reasoning(request, lookup, palette)
        yield from _text_sections("output", "Output", record, _OUTPUT, palette, long_folds=False)
        yield from _tools_called(request, lookup, palette)
        used = set(_OUTPUT)
    elif kind is TrajectoryKind.USER or record.lane is TrajectoryLane.INPUT:
        yield from _text_sections(
            "prompt", "Prompt", record, _OUTPUT | _INPUT, palette, long_folds=False
        )
        used = {*_OUTPUT, *_INPUT}
    else:
        yield from _field_sections("input", "Input", _matching(record.details, _INPUT), palette)
        yield from _field_sections("result", "Result", _matching(record.details, _RESULT), palette)
        yield from _field_sections(
            "payload", "Payload", _matching(record.details, _PAYLOAD), palette
        )
        used = {*_INPUT, *_RESULT, *_PAYLOAD}
        if record.summary and not any(_key(field.name) in used for field in record.details):
            yield _content_section("summary", "Summary", record.summary, palette)
    yield from _other_sections(
        record.details, {field for field in record.details if _key(field.name) in used}, palette
    )


def _text_sections(
    key: str,
    title: str,
    record: TrajectoryRecord,
    aliases: frozenset[str],
    palette: Palette,
    *,
    long_folds: bool = True,
) -> Iterable[Section]:
    """Prose sections: model and user text is markdown unless declared otherwise."""
    fields = _matching(record.details, aliases)
    if fields:
        yield from _field_sections(key, title, fields, palette, long_folds=long_folds, prose=True)
    elif record.summary:
        yield _content_section(
            key, title, record.summary, palette, long_folds=long_folds, prose=True
        )


def _sibling_reasoning(
    request: TrajectoryRequest | None, lookup: RecordLookup, palette: Palette
) -> Iterable[Section]:
    """The request's reasoning, folded, above the output it led to.

    Reasoning a provider withheld (an empty block) is not on the timeline, so its
    time and token count are noted here instead.
    """
    siblings = [
        sibling
        for record_id in (request.model_record_ids if request is not None else ())
        if (sibling := lookup(record_id)) is not None and sibling.kind is TrajectoryKind.REASONING
    ]
    texts = [text for sibling in siblings if (text := _first_text(sibling, _REASONING).strip())]
    if texts:
        text = "\n\n".join(texts)
        yield _content_section("reasoning", "Reasoning", text, palette, folded=True, prose=True)
    elif siblings:
        withheld = ["Not disclosed by the provider"]
        milliseconds = sum(
            sibling.timing.duration_ms or 0 for sibling in siblings if sibling.timing is not None
        )
        if milliseconds:
            withheld.append(format_milliseconds(milliseconds))
        usage = request.usage if request is not None else None
        if usage is not None and usage.reasoning_tokens:
            withheld.append(f"{compact_number(usage.reasoning_tokens)} reasoning tokens")
        note = Text(" · ".join(withheld), style=palette.muted)
        yield Section("reasoning", "Reasoning", note, note.plain, long_folds=False)


def _first_text(record: TrajectoryRecord, aliases: frozenset[str]) -> str:
    fields = _matching(record.details, aliases)
    return fields[0].preview.text if fields else record.summary


def _tools_called(
    request: TrajectoryRequest | None, lookup: RecordLookup, palette: Palette
) -> Iterable[Section]:
    """One clickable line per tool call the request made; Enter on it jumps there."""
    lines: list[Text] = []
    seen: set[str] = set()
    for record_id in request.tool_record_ids if request is not None else ():
        call = lookup(record_id)
        if call is None or call.kind is not TrajectoryKind.TOOL_CALL:
            continue
        identity = call.call_id or record_id
        if identity in seen:
            continue
        seen.add(identity)
        link = Style(meta={DETAIL_RECORD_TARGET_META: record_id})
        line = Text("⚙ ", style=palette.accent + link)
        name = call.mcp_tool or _field_text(call.details, {"tool", "tool_name"}) or call.summary
        line.append(sanitize_text(name or "tool"), style=palette.key + link)
        argument = _input_value(_matching(call.details, _INPUT), TOOL_ROW_INPUT_KEY_PRIORITY)
        if argument:
            line.append(f"  {_one_line(argument)}", style=palette.muted + link)
        lines.append(line)
    if lines:
        body = Text("\n").join(lines)
        yield Section("tools", f"Tools called ({len(lines)})", body, body.plain, long_folds=True)


def _field_text(fields: Iterable[DetailField], names: set[str]) -> str | None:
    return next((field.preview.text for field in fields if _key(field.name) in names), None)


# ---- shared sections ------------------------------------------------------------------


def _key(name: str) -> str:
    return name.casefold().replace("-", "_").replace(" ", "_")


def _matching(fields: Iterable[DetailField], aliases: frozenset[str]) -> tuple[DetailField, ...]:
    return tuple(field for field in fields if _key(field.name) in aliases)


def _field_sections(
    key: str,
    title: str,
    fields: tuple[DetailField, ...],
    palette: Palette,
    *,
    lexer: str | None = None,
    folded: bool = False,
    long_folds: bool = True,
    prose: bool = False,
) -> Iterable[Section]:
    for index, field in enumerate(fields):
        label = title if len(fields) == 1 else f"{title} · {field.name}"
        text = field.preview.text
        fmt = (
            ContentFormat.MARKDOWN if prose and field.format is ContentFormat.TEXT else field.format
        )
        section = f"{key}:{index}"
        yield Section(
            section,
            label,
            _body(text, palette, section, fmt, lexer),
            _copy(text),
            folded=folded,
            long_folds=long_folds,
        )


def _body(
    text: str, palette: Palette, section: str, fmt: ContentFormat, lexer: str | None = None
) -> Callable[[frozenset[str]], RenderableType]:
    def body(toggled: frozenset[str]) -> RenderableType:
        return render_content(
            text, palette, format=fmt, lexer=lexer, scope=f"{section}/", toggled=toggled
        )

    return body


def _content_section(
    key: str,
    title: str,
    text: str,
    palette: Palette,
    *,
    folded: bool = False,
    long_folds: bool = True,
    prose: bool = False,
) -> Section:
    fmt = ContentFormat.MARKDOWN if prose else ContentFormat.TEXT
    body = _body(text, palette, key, fmt)
    return Section(key, title, body, _copy(text), folded=folded, long_folds=long_folds)


def _other_sections(
    fields: Iterable[DetailField], shown: set[DetailField], palette: Palette
) -> Iterable[Section]:
    """Fields no section claimed, so nothing a harness reports is silently dropped."""
    for index, field in enumerate(fields):
        if field in shown or _key(field.name) in _HEADER_FIELDS:
            continue
        key = f"field:{index}"
        body = _body(field.preview.text, palette, key, field.format)
        yield Section(key, field.name, body, _copy(field.preview.text))


def _failure_sections(failure: TrajectoryFailure | None, palette: Palette) -> Iterable[Section]:
    if failure is None:
        return
    data = {
        "category": failure.category.value.replace("_", " "),
        "code": failure.code,
        "detail": failure.detail,
    }
    shown = {name: value for name, value in data.items() if value}
    body = render_data(shown, Palette(**{**_fields(palette), "key": palette.error}))
    yield Section("error", "Error", body, json.dumps(shown, indent=2), long_folds=False)


def _link_sections(links: Iterable[ParticipantLink], palette: Palette) -> Iterable[Section]:
    lines = []
    for link in links:
        style = Style(meta=participant_link_meta(link))
        line = Text(f"{link.relation} ", style=palette.muted + style)
        line.append(link.participant_id, style=palette.accent + Style(underline=True) + style)
        if link.target_record_id is not None:
            line.append("  → exact event", style=palette.muted + style)
        lines.append(line)
    if lines:
        body = Text("\n").join(lines)
        yield Section("participants", "Participants", body, body.plain, long_folds=False)


def _record_debug(record: TrajectoryRecord, request: TrajectoryRequest | None) -> dict:
    return {
        "Record": record.record_id,
        "Request": request.source_request_id or request.request_id if request else None,
        "Call ID": record.call_id,
        "Turn": record.turn_id,
        "Source": record.source,
        "Epoch": record.source_epoch,
        "Timing": record.timing.provenance.value if record.timing is not None else None,
    }


def _debug_section(values: dict[str, str | None], palette: Palette) -> Section:
    shown = {name: value for name, value in values.items() if value}
    return Section(
        "debug", "Debug", render_data(shown, palette), json.dumps(shown, indent=2), folded=True
    )


def _copy(text: str) -> str:
    decoded = unwrap(text) if text.lstrip()[:1] in {"{", "["} else text
    if isinstance(decoded, str):
        return decoded
    return json.dumps(decoded, ensure_ascii=False, indent=2)


def _fields(palette: Palette) -> dict[str, Style]:
    return {name: getattr(palette, name) for name in Palette.__dataclass_fields__}


# ---- header ---------------------------------------------------------------------------


def _title(
    glyph: str,
    name: str,
    subtitle: str | None,
    status: TrajectoryStatus,
    timing: Timing | None,
    palette: Palette,
) -> Text:
    title = Text(no_wrap=True, overflow="ellipsis")
    title.append(f"{glyph} ", style=palette.accent)
    title.append(sanitize_text(name), style=palette.text + Style(bold=True))
    status_style = {
        TrajectoryStatus.COMPLETED: palette.success,
        TrajectoryStatus.ERROR: palette.error,
        TrajectoryStatus.INTERRUPTED: palette.error,
        TrajectoryStatus.RUNNING: palette.accent,
        TrajectoryStatus.PENDING: palette.accent,
    }.get(status, palette.muted)
    title.append(f"   ● {status.value.replace('_', ' ')}", style=status_style)
    if (duration := format_duration(timing)) and duration != "—":
        title.append(f"   {duration}", style=palette.text)
    if timing is not None and timing.start is not None:
        title.append(
            f"   {time.strftime('%H:%M:%S', time.localtime(timing.start))}", style=palette.muted
        )
    if subtitle:
        title.append(f"   {_one_line(subtitle)}", style=palette.muted)
    return title


def _usage_line(
    usage: TrajectoryUsage | None, request: TrajectoryRequest | None, palette: Palette
) -> Text | None:
    """Provider, model, tokens, cost, and first-token latency of the model request."""
    model = (usage.model if usage else None) or (request.model if request else None)
    provider = (usage.provider if usage else None) or (request.provider if request else None)
    parts: list[tuple[str, Style]] = []
    if model:
        label = model if not provider or model.startswith(f"{provider}/") else f"{provider}/{model}"
        parts.append((sanitize_text(label), palette.key))
    if usage is not None:
        cache = usage.cache_read_tokens + usage.cache_write_tokens
        for name, value in (
            ("in", usage.input_tokens),
            ("out", usage.output_tokens),
            ("reasoning", usage.reasoning_tokens),
            ("cache", cache),
        ):
            if value:
                parts.append((f"{name} {compact_number(value)}", palette.text))
        if usage.cost_usd is not None:
            parts.append((f"${compact_cost(usage.cost_usd)}", palette.number))
    if request is not None and request.ttft_ms is not None:
        parts.append((f"first token {format_milliseconds(request.ttft_ms)}", palette.muted))
    if not parts:
        return None
    line = Text(no_wrap=True, overflow="ellipsis")
    for index, (part, style) in enumerate(parts):
        if index:
            line.append("  ·  ", style=palette.muted)
        line.append(part, style=style)
    return line


def _one_line(value: str) -> str:
    line = " ".join(sanitize_text(value).split())
    limit = TOOL_ROW_INPUT_VALUE_MAX_CHARS
    return line if len(line) <= limit else f"{line[: limit - 1]}…"


__all__ = ["RecordLookup", "Section", "SpanSheet", "build_sheet"]
