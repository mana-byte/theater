"""The evidence block a failed live cell leaves for the job log, printed before the sandbox dies."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

from ..diagnostics import _ANSI, _CONTROL

BLOCK_CHARS = 12 * 1024
RELEASE_CHARS = 96 * 1024
LINE_CHARS = 300
CAUSE_CHARS = 2000
_TAIL_BYTES = 256 * 1024
_LOG_CHARS = 200
_DAEMON_PROBLEMS = 5
_DAEMON_RECORDS = 15
_LOG_TAIL = 10
_PANE_LINES = 40
_RECORD = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(\S+) (.*)$"
)
# theater.timing is most of the DEBUG volume; the pricing warning is not a failure cause.
_SKIP_LOGGERS = ("theater.timing", "theater.pricing.estimation")
# Safety net on top of the ANSI/control scrub: tokens that must never reach a public log.
_REDACTED = (
    (re.compile(r"regie-provider-v1\.\S+"), "regie-provider-v1.<redacted>"),
    (re.compile(r"register-\S+"), "register-<redacted>"),
    (re.compile(r"Bearer \S+"), "Bearer <redacted>"),
    (re.compile(r"gh[ps]_\S+"), "<redacted>"),
    (re.compile(r"github_pat_\S+"), "<redacted>"),
)


def write_block(
    blocks: Path,
    *,
    harness: str,
    version: str,
    cell: str,
    outcome: str,
    cause: str,
    evidence: Path,
) -> None:
    """Write one cell's printable block under ``blocks``; never raise into the suite."""
    with contextlib.suppress(Exception):
        slug = re.sub(r"[^a-z0-9]+", "-", cell.lower()).strip("-")[:80] or "cell"
        head = [
            _scrub(f"::group::compat:evidence {harness} {version} {cell} {outcome}")[:LINE_CHARS],
            _scrub(f"compat:evidence:begin {harness} {version} {cell}")[:LINE_CHARS],
        ]
        foot = [
            _scrub(f"compat:evidence:end {harness} {version} {cell}")[:LINE_CHARS],
            "::endgroup::",
        ]
        causes = _cause_lines(cause)
        body = causes + [f"| {_scrub(line)}"[:LINE_CHARS] for line in _evidence_lines(evidence)]
        text = "\n".join([*head, *body, *foot]) + "\n"
        while len(body) > len(causes) and len(text.encode("utf-8")) > BLOCK_CHARS:
            body.pop()
            text = "\n".join([*head, *body, *foot]) + "\n"
        used = 0
        if blocks.is_dir():
            for old in blocks.glob("*.txt"):
                with contextlib.suppress(OSError):
                    used += old.stat().st_size
        if used + len(text.encode("utf-8")) > RELEASE_CHARS:
            text = "\n".join([*head, *causes, *foot]) + "\n"
        blocks.mkdir(parents=True, exist_ok=True)
        (blocks / f"{slug}.txt").write_text(text, encoding="utf-8")


def print_blocks(blocks: Path) -> None:
    """Print every block the suite left, in cell order; a missing directory prints nothing."""
    if not blocks.is_dir():
        return
    for path in sorted(blocks.glob("*.txt")):
        with contextlib.suppress(OSError):
            if text := path.read_text(encoding="utf-8", errors="replace"):
                print(text, end="", flush=True)


def _evidence_lines(evidence: Path) -> list[str]:
    """The cell's captured facts in report order: rig facts, logs, transcript, panes."""
    rig = [line for line in _tail(evidence / "rig.txt") if line.strip()]
    return [
        *[line for line in rig if not line.startswith("transcript:")],
        *_daemon_lines(_captured(evidence, "daemon.log")),
        *_log_lines(_captured(evidence, "bridge.log"), "bridge.log", _LOG_TAIL),
        *_stderr_lines(_captured(evidence, "daemon/stderr/*.log")),
        *[line for line in rig if line.startswith("transcript:")],
        *_pane_lines(evidence),
    ]


def _captured(evidence: Path, pattern: str) -> list[Path]:
    """Captured files matching ``pattern``: the live stack's copies, else a boot attempt's."""
    found = sorted(evidence.rglob(pattern))
    current = [p for p in found if not str(p.relative_to(evidence)).startswith("boot")]
    return current or found


def _cause_lines(cause: str) -> list[str]:
    """The scrubbed cause, chunked to line width, within CAUSE_CHARS of content."""
    width = LINE_CHARS - len("| cause: ")
    text = "\n".join(_scrub(line) for line in cause.splitlines())[:CAUSE_CHARS]
    lines: list[str] = []
    for source in text.split("\n"):
        lines += [
            f"| cause: {source[start : start + width]}" for start in range(0, len(source), width)
        ]
    return lines or ["| cause: (empty)"]


def _scrub(line: str) -> str:
    """Strip ANSI/control bytes and redact tokens, keeping whitespace."""
    line = _CONTROL.sub("", _ANSI.sub("", line))
    for pattern, replacement in _REDACTED:
        line = pattern.sub(replacement, line)
    return line


def _tail(path: Path) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - _TAIL_BYTES))
            return handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _daemon_lines(logs: list[Path]) -> list[str]:
    """DEBUG+ records of the daemon log: newest problems first, then the last records."""
    if not logs:
        return []
    records = [m.groups() for line in _tail(logs[0]) if (m := _RECORD.match(line))]
    kept = [r for r in records if r[1] not in _SKIP_LOGGERS]
    problems = [r for r in kept if r[0] in ("WARNING", "ERROR", "CRITICAL")][-_DAEMON_PROBLEMS:]
    shown: list[str] = []
    seen: set[str] = set()
    for level, logger, text in [*reversed(problems), *kept[-_DAEMON_RECORDS:]]:
        line = f"daemon.log: {level} {logger} {text}"[:_LOG_CHARS]
        if line not in seen:
            seen.add(line)
            shown.append(line)
    return shown


def _log_lines(logs: list[Path], label: str, records: int) -> list[str]:
    if not logs:
        return []
    lines = [line for line in _tail(logs[0]) if line.strip()][-records:]
    return [f"{label}: {line}"[:_LOG_CHARS] for line in lines]


def _stderr_lines(logs: list[Path]) -> list[str]:
    """Raw crash output of daemon workers that died before logging was configured."""
    lines = [f"stderr/{p.name}: {line}" for p in logs for line in _tail(p) if line.strip()]
    return [line[:_LOG_CHARS] for line in lines[-_LOG_TAIL:]]


def _pane_lines(evidence: Path) -> list[str]:
    lines = [
        f"{p.stem}: {line}"
        for p in sorted(evidence.glob("pane-*.txt"))
        for line in _tail(p)
        if line.strip()
    ]
    return lines[-_PANE_LINES:]
