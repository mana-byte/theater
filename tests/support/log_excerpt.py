"""The cause a failed scenario's captured logs hold, short enough to survive CI clipping."""

from __future__ import annotations

import re
from pathlib import Path

EXCERPT_CHARS = 500
_RECORDS = 3
_TAIL_BYTES = 64 * 1024
_STAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} ")
_PROBLEM = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (WARNING|ERROR|CRITICAL)\s+(.*)$")


def log_excerpt(evidence: Path, *, limit: int = EXCERPT_CHARS) -> str:
    """The latest warnings and errors of each captured log, newest first, each log with an equal
    share of ``limit``.

    The sandbox is deleted with the logs, and a report clips a failure to its assertion text.
    Newest first keeps a clip from dropping the decisive record, an equal share keeps a noisy log
    from starving another, and a record another log already showed (the daemon's own output is
    in two files) is shown once. An unreadable log adds nothing.
    """
    seen: set[str] = set()
    shown: list[str] = []
    for log in sorted(evidence.rglob("*.log")):
        try:
            with log.open("rb") as handle:
                handle.seek(max(0, log.stat().st_size - _TAIL_BYTES))
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        # A log with no timestamped record is raw output (a crash before logging was configured).
        found = _problems(lines) if any(map(_STAMP.match, lines)) else _raw(lines)
        records = [record for record in found if record not in seen]
        seen.update(records)
        if records:
            shown.append(f"{log.relative_to(evidence).as_posix()}: {' | '.join(records)}")
    return " ; ".join(entry[: limit // len(shown)] for entry in shown)


def _raw(lines: list[str]) -> list[str]:
    return [line.strip()[:200] for line in reversed(lines) if line.strip()][:_RECORDS]


def _problems(lines: list[str]) -> list[str]:
    """Distinct warning and error records, newest first, without their date. A record keeps its
    last continuation line: where a logged traceback names the exception."""
    records: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if _STAMP.match(line):
            problem = _PROBLEM.match(line)
            current = [f"{problem[1]} {problem[2]}"[:200], ""] if problem else None
            if current is not None:
                records.append(current)
        elif current is not None and line.strip():
            current[1] = line.strip()[:120]
    picked: dict[str, str] = {}
    for header, tail in reversed(records):
        if len(picked) == _RECORDS:
            break
        picked.setdefault(header, tail)
    return [f"{header} … {tail}" if tail else header for header, tail in picked.items()]
