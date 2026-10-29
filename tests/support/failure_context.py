"""Print orchestration state into failed tests' report sections, so CI flakes carry evidence."""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import sqlite3
from collections import deque
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

#: Cap for one failure-context section; a flake dump must fit in the job log.
_MAX_SECTION_CHARS = 8000
#: Cap on how many failures per worker get a dump.
_MAX_DUMPS_PER_WORKER = 5
#: How many recent node ids this worker ran.
_NODE_ID_HISTORY = 25
#: Rows per table, log lines per file, log files per dump.
_ROW_LIMIT = 30
_LOG_TAIL_LINES = 40
_LOG_FILES = 5

_recent_node_ids: deque[str] = deque(maxlen=_NODE_ID_HISTORY)
# itertools.count needs no `global` (PLW0603); each failed report consumes one value.
_dump_counter = itertools.count()


def pytest_configure(config: pytest.Config) -> None:
    # log_level=INFO lets alembic print ~41 migration records per fresh Store, burying
    # the daemon's own lines; migration chatter is not failure evidence.
    logging.getLogger("alembic").setLevel(logging.WARNING)


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    _recent_node_ids.append(nodeid)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[Any]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    # A diagnostic hook must never abort the run; a prototype hit INTERNALERROR here.
    with contextlib.suppress(Exception):
        _append_context(item, report)
    return report


def _append_context(item: pytest.Item, report: pytest.TestReport) -> None:
    # Teardown state is already gone; call-phase makereport runs before fixture teardown.
    if not report.failed or report.when not in ("setup", "call"):
        return
    if next(_dump_counter) >= _MAX_DUMPS_PER_WORKER:
        return
    text = _neutralise(_collect(item))[:_MAX_SECTION_CHARS]
    report.sections.append(("Theater failure context", text))


def _collect(item: pytest.Item) -> str:
    parts = [f"node ids (last {_NODE_ID_HISTORY} this worker ran):"]
    parts.extend(f"  {nodeid}" for nodeid in _recent_node_ids)
    home = os.environ.get("THEATER_HOME")
    if home:
        root = Path(home)
        parts.append(_db_dump(root / "var" / "state" / "theater.db"))
        parts.append(_log_tail(root / "var" / "logs" / "daemon"))
    parts.append(_provider_state(item))
    return "\n".join(part for part in parts if part)


def _db_dump(db: Path) -> str:
    if not db.is_file():
        return ""
    try:
        # Read-only URI: works after the daemon closed and never creates or migrates.
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        parts = [
            _rows(
                conn,
                "bus (newest first)",
                # agent.* payloads carry real transcripts in live runs; never print them.
                "SELECT id, ts, from_id, to_id, kind,"
                " CASE WHEN kind LIKE 'agent.%' THEN NULL ELSE payload END AS payload"
                " FROM bus ORDER BY id DESC LIMIT ?",
            ),
            # prompt/result/structured_* are never selected.
            _rows(
                conn,
                "jobs (newest first)",
                "SELECT handle, kind, state, error_code, created_at, finished_at"
                " FROM jobs ORDER BY created_at DESC LIMIT ?",
            ),
            # payload/error are never selected.
            _rows(
                conn,
                "control_operations (newest first)",
                "SELECT operation_id, participant_id, kind, queue_sequence, delivery_phase,"
                " delivery_result, execution_barrier, error_code"
                " FROM control_operations ORDER BY created_at DESC LIMIT ?",
            ),
        ]
    finally:
        conn.close()
    return "\n".join(part for part in parts if part)


def _rows(conn: sqlite3.Connection, title: str, sql: str) -> str:
    try:
        cursor = conn.execute(sql, (_ROW_LIMIT,))
        names = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
    except sqlite3.Error:
        return f"{title}: <unavailable>"  # schema coupling must not fail the hook
    lines = [f"{title} ({len(rows)} rows):"]
    lines.extend(
        "  " + " ".join(f"{name}={value!r}" for name, value in zip(names, row, strict=True))
        for row in rows
    )
    return "\n".join(lines)


def _provider_state(item: pytest.Item) -> str:
    provider = getattr(item, "funcargs", {}).get("terminal_provider")
    if provider is None:
        return ""
    # creations[*]["env"] is never printed.
    creations = [
        f"    name={c.get('name')!r} argv0={c['command'][0] if c.get('command') else None!r}"
        f" cwd={c.get('cwd')!r}"
        for c in provider.creations
    ]
    lines = [
        "FakeTerminalProvider:",
        f"  deliveries[-8:]: {provider.deliveries[-8:]!r}",
        f"  interruptions: {provider.interruptions!r}",
        f"  terminations: {provider.terminations!r}",
        f"  presence: {provider.presence!r}",
        "  creations (name, argv0, cwd):",
        *creations,
    ]
    return "\n".join(lines)


def _log_tail(logs_dir: Path) -> str:
    if not logs_dir.is_dir():
        return ""
    lines = [f"daemon log tail ({logs_dir}):"]
    for path in sorted(logs_dir.rglob("*.log"))[:_LOG_FILES]:
        try:
            content = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        lines.append(f"  {path.name}:")
        lines.extend(f"    {line}" for line in content[-_LOG_TAIL_LINES:])
    return "\n".join(lines)


def _neutralise(text: str) -> str:
    # Replayed log text may hold runner workflow commands (::name::) at any indentation.
    return "\n".join(
        line.replace("::", ";;") if line.lstrip().startswith("::") else line
        for line in text.splitlines()
    )
