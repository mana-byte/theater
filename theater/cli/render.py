"""Pure terminal formatting for the CLI.

No daemon calls, no tmux, no side effects.  Imported by ``cli/__init__.py``
for use in command handlers that need to render participants, bus events,
candidates, coverage, bytes, and model lists.
"""

from __future__ import annotations

import json
import shutil
import time

from theater.formatting import (
    TIER_LEGEND,
    clip_harness,
    clip_name,
    event_stamp,
    event_summary,
    event_who,
    flatten_tree,
    reach_mark,
    tier_mark,
    tilde,
)
from theater.harness import harness_icon


def _width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def _row_line(p: dict, indent: int = 0) -> str:
    pad = "  " * indent
    return (
        f"{p['id']:<14}{tier_mark(p['tier'])}{reach_mark(p['addressable'])} "
        f"{clip_name(p.get('name')):<12} "
        f"{harness_icon(p.get('harness'))} "
        f"{clip_harness(p.get('harness')):<11} "
        f"{p['status']:<15} {p.get('tmux_pane') or '-':<6} {pad}{tilde(p.get('cwd'))}"
    )


def _format_ls(rows: list[dict], *, tree: bool, unmanaged: list[dict] | None = None) -> str:
    if not rows and not unmanaged:
        return "no participants"
    body = flatten_tree(rows, _row_line) if tree else [_row_line(r) for r in rows]
    header = (
        f"{'ID':<14}{'T':<2} {'NAME':<12}   {'HARNESS':<11} {'STATUS':<15} {'PANE':<6} DIRECTORY"
    )
    lines = [header, *body]
    if unmanaged:
        lines.append("")
        lines.append("unmanaged (harness panes not yet adopted):")
        for u in unmanaged:
            cmd = clip_harness(u.get("command"))
            pane = u.get("pane") or "-"
            icon = harness_icon(u.get("harness") or u.get("command"))
            lines.append(
                f"  {'-':<12}{'?':<2} {'-':<12} {icon} {cmd:<11} "
                f"{'-':<15} {pane:<6} {tilde(u.get('cwd'))}"
            )
    lines.extend(["", TIER_LEGEND])
    return "\n".join(lines)


def _bus_line(row: dict, width: int) -> str:
    line = (
        f"{event_stamp(row.get('ts'))}  {row.get('kind', '?'):<18} "
        f"{event_who(row):<32} {event_summary(row.get('payload'))}"
    )
    return line[: width - 1] if width > 20 and len(line) >= width else line


def _matching(rows: list[dict], prefix: str | None) -> list[dict]:
    if not prefix:
        return rows
    return [r for r in rows if str(r.get("kind", "")).startswith(prefix)]


def _candidate_line(row: dict) -> str:
    owner = row.get("owner") or "-"
    tombstone = row.get("tombstone") or "-"
    reason = row.get("rejection_reason") or "-"
    size = row.get("size")
    mtime = row.get("mtime")
    stamp = event_stamp(mtime) if mtime else "-"
    size_text = _format_bytes(size) if isinstance(size, int) else "-"
    return (
        f"{row.get('location'):<72} {row.get('session_id') or '-':<36} "
        f"{stamp:<17} {size_text:>9} {owner:<14} {tombstone:<14} {reason}"
    )


def _format_bytes(n: int) -> str:
    """A human-readable byte count, because raw bytes are unreadable on a CLI."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _format_floor(ts: float | None) -> str:
    """Render a retention-floor timestamp, or say plainly that there is none."""
    if ts is None:
        return "no data"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _models_block(harness: str, models: list[str], section_name: str) -> str:
    """Render a `[models]` entry the user can paste without editing."""
    lines = [f"[{section_name}]", f"{harness} = ["]
    lines += [f"  {json.dumps(name)}," for name in models]
    lines.append("]")
    return "\n".join(lines)
