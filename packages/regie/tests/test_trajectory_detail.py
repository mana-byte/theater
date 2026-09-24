from __future__ import annotations

import json

import pytest
from regie.trajectory.domain import (
    ParticipantLink,
    TrajectoryRecord,
    requests_for_records,
)
from regie.trajectory.rich.inspection.content import Palette, lenient_json, render_content
from regie.trajectory.rich.inspection.links import participant_link_from_meta
from regie.trajectory.rich.inspection.sheet import build_sheet
from regie.trajectory.rich.render.tools import build_tool_index
from regie.trajectory.rich.widgets.span_detail import SpanDetailPanel
from regie.trajectory.ui_constants import TRAJECTORY_DETAIL_FOLD_LINES
from rich.console import Console
from textual.app import App, ComposeResult
from textual.widgets import RichLog


def _record(record_id: str, kind: str, lane: str, **fields: object) -> TrajectoryRecord:
    details = fields.pop("details", {})
    return TrajectoryRecord.from_wire(
        {
            "record_id": record_id,
            "revision": 1,
            "participant_id": "p1",
            "source_epoch": "epoch",
            "lane": lane,
            "kind": kind,
            "source": "claude",
            "summary": fields.pop("summary", ""),
            "status": "completed",
            "raw_index": int(record_id[-1]),
            "details": [
                {"name": name, "format": fmt, "value": {"text": text, "omitted_bytes": 0}}
                for name, (fmt, text) in details.items()
            ],
            **fields,
        }
    )


def _plain(renderable: object) -> str:
    console = Console(width=100, color_system=None)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_mcp_results_unwrap_nested_json_even_when_cut_in_the_middle() -> None:
    inner = '{"data": [{"name": "a"}, {"name": "b… 900 bytes omitted …"}]}'
    envelope = json.dumps({"_meta": {"noise": 1}, "content": [{"type": "text", "text": inner}]})

    rendered = _plain(render_content(envelope, Palette()))

    assert "data:" in rendered and "• name: a" in rendered  # decoded, not an escaped string
    assert "… 900 bytes omitted" in rendered  # the cut is labelled, not hidden
    assert "_meta" not in rendered  # protocol envelope noise is dropped
    assert lenient_json('[1, 2, {"k"') == [1, 2, "… truncated"]


def test_escaped_terminal_colours_render_without_their_backgrounds() -> None:
    text = render_content("\\x1b[31;44mred\\x1b[0m done", Palette())

    assert text.plain == "red done"  # type: ignore[union-attr]
    assert all(span.style.bgcolor is None for span in text.spans)  # type: ignore[union-attr]
    assert any(span.style.color is not None for span in text.spans)  # type: ignore[union-attr]


def test_tool_sheet_puts_input_above_result_with_the_command_in_the_header() -> None:
    call = _record(
        "call1",
        "tool_call",
        "tools",
        call_id="c1",
        details={"tool": ("text", "bash"), "arguments": ("json", '{"command": "ls -la"}')},
    )
    result = _record(
        "res2", "tool_result", "tools", call_id="c1", details={"result": ("text", "a\nb")}
    )
    tool = build_tool_index((call, result)).ordered[0]

    sheet = build_sheet(call, Palette(), tool=tool)

    assert [section.title for section in sheet.sections] == ["Input", "Result", "Debug"]
    assert "bash" in sheet.title.plain and "ls -la" in sheet.title.plain
    assert sheet.meta is None  # usage belongs to model spans only
    assert sheet.sections[-1].folded


def test_model_sheet_leads_with_usage_and_folds_the_request_reasoning() -> None:
    usage = {
        "model": "claude",
        "provider": "anthropic",
        "input_tokens": 1200,
        "output_tokens": 40,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.25,
    }
    reasoning = _record("rsn1", "reasoning", "model", request_id="q", summary="think first")
    output = _record("out2", "assistant", "model", request_id="q", summary="**done**", usage=usage)
    call = _record(
        "cal3",
        "tool_call",
        "tools",
        request_id="q",
        call_id="c",
        details={"tool": ("text", "read")},
    )
    records = {record.record_id: record for record in (reasoning, output, call)}
    request = requests_for_records(records.values())[0]

    sheet = build_sheet(output, Palette(), request=request, lookup=records.get)
    titles = [section.title for section in sheet.sections]

    assert sheet.meta is not None
    assert all(part in sheet.meta.plain for part in ("anthropic/claude", "in 1.2K", "$0.25"))
    assert titles[:3] == ["Reasoning", "Output", "Tools called (1)"]
    assert sheet.sections[0].folded and not sheet.sections[1].folded


def test_participant_links_keep_their_exact_target_in_clickable_metadata() -> None:
    link = ParticipantLink("p2", "child", target_record_id="target")
    record = _record("spn1", "spawn", "theater", links=[link.to_wire()])

    section = next(
        section
        for section in build_sheet(record, Palette()).sections
        if section.title == "Participants"
    )
    metas = [span.style.meta for span in section.body.spans if span.style.meta]  # type: ignore[union-attr]

    assert participant_link_from_meta(metas[0]) == link


class _Host(App):
    def compose(self) -> ComposeResult:
        yield SpanDetailPanel(id="panel")


@pytest.mark.parametrize("theme", ["textual-dark", "textual-light", "gruvbox"])
async def test_canvas_navigates_folds_and_copies_sections_on_any_theme(theme: str) -> None:
    long_result = "\n".join(f"line {index}" for index in range(60))
    call = _record(
        "call1", "tool_call", "tools", call_id="c1", details={"arguments": ("json", '{"n": 1}')}
    )
    result = _record(
        "res2", "tool_result", "tools", call_id="c1", details={"result": ("text", long_result)}
    )
    tool = build_tool_index((call, result)).ordered[0]
    app = _Host()
    app.theme = theme
    async with app.run_test(size=(100, 60)) as pilot:
        panel = app.query_one(SpanDetailPanel)
        panel.set_span(call, tool=tool)
        await pilot.pause()
        log = panel.query_one(RichLog)
        page = "\n".join(strip.text for strip in log.lines)
        headings = {item.line for item in panel._items}

        assert all(  # content never paints over the panel's background
            segment.style is None or segment.style.bgcolor is None
            for index, strip in enumerate(log.lines)
            if index not in headings
            for segment in strip
        )
        tints = {list(log.lines[line])[-1].style.bgcolor for line in sorted(headings)[:2]}
        assert len(tints) == 2  # Input and Result headings are tinted differently
        assert f"line {TRAJECTORY_DETAIL_FOLD_LINES - 1}" in page
        assert f"line {TRAJECTORY_DETAIL_FOLD_LINES}" not in page  # long results fold
        assert "more lines" in page

        panel.move(1)
        assert panel.selected_section is not None
        assert panel.selected_section.title == "Result"
        assert panel.copy_text == long_result
        panel.toggle()  # a partly shown section expands first
        await pilot.pause()
        assert "line 59" in "\n".join(strip.text for strip in log.lines)
        panel.toggle()  # then folds
        await pilot.pause()
        assert "line 0" not in "\n".join(strip.text for strip in log.lines)
        assert "## Input" in panel.page_copy_text and "## Result" in panel.page_copy_text


async def test_long_data_branches_are_folded_items_the_cursor_can_open() -> None:
    data = json.dumps({"rows": [{"id": index} for index in range(30)], "total": 30})
    call = _record("call1", "tool_call", "tools", call_id="c1")
    result = _record(
        "res2", "tool_result", "tools", call_id="c1", details={"result": ("json", data)}
    )
    tool = build_tool_index((call, result)).ordered[0]
    async with _Host().run_test(size=(100, 60)) as pilot:
        panel = pilot.app.query_one(SpanDetailPanel)
        panel.set_span(call, tool=tool)
        await pilot.pause()
        page = "\n".join(strip.text for strip in panel.query_one(RichLog).lines)
        assert "▸ rows:  30 lines" in page and "total: 30" in page and "id: 29" not in page

        panel.move(1)  # from the Result heading onto its folded "rows" branch
        assert panel._items[panel._cursor].node is not None
        panel.toggle()
        await pilot.pause()
        page = "\n".join(strip.text for strip in panel.query_one(RichLog).lines)
        assert "▾ rows:" in page and "id: 29" in page


def test_yaml_toml_and_markdown_render_as_what_they_are() -> None:
    yaml_text = _plain(
        render_content("name: web\nservice:\n  port: 80\n  debug: true\n", Palette())
    )
    toml_text = _plain(render_content('[tool]\nname = "x"\n[tool.opts]\nlevel = 3\n', Palette()))
    markdown = _plain(render_content("# Title\n\n- **one**\n", Palette()))

    assert "service:\n  port: 80\n  debug: true" in yaml_text
    assert "tool:\n  name: x\n  opts:\n    level: 3" in toml_text
    assert markdown.lstrip().startswith("Title") and "• one" in markdown and "**" not in markdown


def test_withheld_reasoning_is_noted_on_the_output_it_led_to() -> None:
    timing = {"start": 0.0, "end": 1.5, "duration_ms": 1500.0, "provenance": "source"}
    usage = {
        "model": "claude",
        "input_tokens": 1,
        "output_tokens": 2,
        "reasoning_tokens": 118,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    reasoning = _record("rsn1", "reasoning", "model", request_id="q", timing=timing)
    output = _record("out2", "assistant", "model", request_id="q", summary="ok", usage=usage)
    records = {record.record_id: record for record in (reasoning, output)}
    request = requests_for_records(records.values())[0]

    sheet = build_sheet(output, Palette(), request=request, lookup=records.get)

    assert sheet.sections[0].title == "Reasoning"
    assert sheet.sections[0].copy_text == (
        "Not disclosed by the provider · 1.5s · 118 reasoning tokens"
    )


async def test_clicking_more_lines_expands_the_section() -> None:
    long_result = "\n".join(f"line {index}" for index in range(60))
    call = _record("call1", "tool_call", "tools", call_id="c1")
    result = _record(
        "res2", "tool_result", "tools", call_id="c1", details={"result": ("text", long_result)}
    )
    tool = build_tool_index((call, result)).ordered[0]
    async with _Host().run_test(size=(100, 60)) as pilot:
        panel = pilot.app.query_one(SpanDetailPanel)
        panel.set_span(call, tool=tool)
        await pilot.pause()
        log = panel.query_one(RichLog)
        row = next(index for index, strip in enumerate(log.lines) if "more lines" in strip.text)

        await pilot.click(log, offset=(8, row + log.content_region.y - log.region.y))
        await pilot.pause()

        assert "line 59" in "\n".join(strip.text for strip in log.lines)
