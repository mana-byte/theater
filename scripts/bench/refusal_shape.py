"""Measure what a model actually sees when Theater refuses a call (WP-B3).

Drives the MCP server in-process against a real daemon under a temporary THEATER_HOME, triggers
real refusals, and applies a vibe shim: an isError result shows only vibe's fixed generic text.
Other harnesses (codex here) show the full content text.
Run the same file against the base commit for "before":
  uv run --project <base-worktree> python scripts/bench/refusal_shape.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path

VIBE_GENERIC = "The MCP tool returned an error"
HISTORY = Path("/tmp/theater-trajectory-analysis/data/full/vibe.jsonl")
HARNESSES = ("vibe", "codex")


def _visible(result, *, vibe_shim: bool) -> str:
    """The text the model reads; vibe drops isError content for a generic message."""
    if vibe_shim and result.is_error:
        return VIBE_GENERIC
    return "\n".join(getattr(block, "text", "") for block in result.content)


async def _call(server, tool: str, arguments: dict):
    """Exactly what the SDK's tools/call handler sends: an exception becomes isError text."""
    from mcp.types import CallToolResult, TextContent

    try:
        return await server.call_tool(tool, arguments)
    except Exception as exc:
        return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)


class _Labels(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def _metric_label(result) -> str:
    """Run the real ToolTiming middleware over a result and read its error_type label."""
    from mcp.server.context import ServerRequestContext

    from theater.mcp.instrumentation import ToolTiming

    handler = _Labels()
    logger = logging.getLogger("theater.timing")
    logger.addHandler(handler)
    previous, propagate = logger.level, logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        context = ServerRequestContext(
            session=None,
            lifespan_context={},
            protocol_version="2025-11-25",
            method="tools/call",
            params={"name": "send", "arguments": {}},
        )

        async def call_next(_ctx):
            return result

        await ToolTiming(frozenset({"send"}), "control")(context, call_next)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
        logger.propagate = propagate
    for line in handler.lines:
        if match := re.search(r"error_type=(\S+)", line):
            return match.group(1)
    return "none(success)"


async def _refusals(build) -> dict[str, dict]:
    """Trigger each refusal once per harness; returns visible text stats and metric labels."""
    out: dict[str, dict] = {}
    for harness in HARNESSES:
        other_id, name = f"other-{harness}", f"Scapin-{harness}"
        me = build(f"me-{harness}", harness)
        other = build(other_id, harness)
        await _call(me, "whoami", {})
        await _call(other, "whoami", {})
        await _call(other, "update_participant", {"name": name})
        cases = {
            "send_missing_target": (me, "send", {"target": "nobody", "prompt": "hi"}),
            "read_transcript_missing": (me, "read_transcript", {"target": "nobody"}),
            "send_unaddressable": (me, "send", {"target": other_id, "prompt": "hi"}),
            "name_taken": (me, "update_participant", {"name": name}),
            "bad_harness_value": (
                me,
                "spawn_session",
                {"harness": "cursor", "prompt": "hi", "approval": "manual"},
            ),
            "load_missing_skill": (me, "load_skill", {"name": "no-such-skill"}),
        }
        for label, (server, tool, arguments) in cases.items():
            result = await _call(server, tool, arguments)
            seen = _visible(result, vibe_shim=harness == "vibe")
            full = _visible(result, vibe_shim=False)
            reason = 0 if seen == VIBE_GENERIC else len(seen)
            out[f"{label}[{harness}]"] = {
                "is_error": bool(result.is_error),
                "model_visible_reason_chars": reason,
                "visible_reason_chars_full": len(full),
                "has_next": '"next"' in full,
                "metric_error_type": await _metric_label(result),
            }
    return out


def _history() -> list[tuple[str, dict]]:
    """(tool, arguments) of every historical vibe Theater call."""
    rows = []
    if not HISTORY.exists():
        return rows
    for line in HISTORY.read_text().splitlines():
        row = json.loads(line)
        if row.get("k") != "theater_call":
            continue
        try:
            arguments = json.loads(row["d"].get("arguments") or "{}")
        except ValueError:
            continue
        if isinstance(arguments, dict):
            rows.append((str(row["d"].get("tool", "")).removeprefix("mcp_theater."), arguments))
    return rows


async def _ignored_arguments(build) -> dict:
    """Replay historical vibe argument keys against each tool's schema; probe real behaviour."""
    server = build("me-vibe", "vibe")
    schemas = {
        tool.name: (set(tool.input_schema.get("properties", {})), tool.output_schema)
        for tool in await server.list_tools()
    }
    probe = await _call(server, "whoami", {"tail": 5})
    reports = "ignored_arguments" in _visible(probe, vibe_shim=False)
    calls = with_extras = dict_results = 0
    names: Counter[str] = Counter()
    for tool, arguments in _history():
        if tool not in schemas:
            continue
        calls += 1
        extras = set(arguments) - schemas[tool][0]
        if extras:
            with_extras += 1
            names.update(extras)
            dict_results += schemas[tool][1] is None
    return {
        "historical_calls": calls,
        "calls_with_unknown_keys": with_extras,
        "server_reports_ignored_arguments": reports,
        "calls_now_carrying_ignored_arguments": dict_results if reports else 0,
        "unknown_key_counts": dict(names.most_common()),
    }


async def measure() -> dict:
    home = Path(tempfile.mkdtemp(prefix="thb3-", dir="/tmp"))
    os.environ["THEATER_HOME"] = str(home)
    try:
        from theater import harness, paths
        from theater.config import Config
        from theater.daemon.server import Daemon
        from theater.mcp.server import build

        paths.ensure_home()
        harness.install(Config(), local_dir=Path("/nonexistent/theater-harnesses"))
        daemon = Daemon()
        await daemon.start()
        try:
            refusals = await _refusals(build)
            ignored = await _ignored_arguments(build)
        finally:
            await daemon.aclose()
        return {"refusals": refusals, "ignored_arguments": ignored}
    finally:
        shutil.rmtree(home, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the raw measurement")
    parser.add_argument("--before", type=Path, help="JSON from a base-commit run to compare")
    args = parser.parse_args()
    result = asyncio.run(measure())
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True))
        return
    before = json.loads(args.before.read_text()) if args.before else None
    print(
        f"{'refusal[harness]':40} {'model-visible chars':>20} {'isError':>9} {'metric label':>28}"
    )
    for name, row in result["refusals"].items():
        old = before["refusals"].get(name) if before else None
        chars = row["model_visible_reason_chars"]
        label = row["metric_error_type"]
        if old:
            chars = f"{old['model_visible_reason_chars']} -> {chars}"
            label = f"{old['metric_error_type']} -> {label}"
        print(f"{name:40} {chars!s:>20} {row['is_error']!s:>9} {label:>28}")
    ignored = result["ignored_arguments"]
    old_ignored = before["ignored_arguments"] if before else None
    print()
    for key in (
        "historical_calls",
        "calls_with_unknown_keys",
        "calls_now_carrying_ignored_arguments",
    ):
        value = ignored[key]
        if old_ignored:
            value = f"{old_ignored[key]} -> {value}"
        print(f"{key:40} {value}")
    print(f"{'unknown_key_counts':40} {ignored['unknown_key_counts']}")


if __name__ == "__main__":
    main()
