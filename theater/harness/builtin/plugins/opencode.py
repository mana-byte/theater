"""OpenCode.

The first adapter whose output is not a file. It writes nothing per session:
everything lands in one SQLite database shared by every session on the machine,
so there is no transcript to tail, no byte offset to hold, and the four methods
the other three adapters are made of have nothing to say. What it has instead is
an append-only `event` table with a per-session monotonic `seq`, which is a
better cursor than a byte offset — hence `open_source` and `OpenCodeSource`
below, and hence commit 1 of this release.

Launch lever
------------
`$OPENCODE_CONFIG` points at a config file that is **merged** into the user's
own rather than replacing it. Verified with `opencode debug config`: the user's
model, providers and seven existing MCP servers all survived, with our `theater`
entry added. So the same policy as the other three adapters holds — we register
one server and touch nothing else — and, importantly, we never write to
`~/.config/opencode/opencode.jsonc`, which is JSONC and would lose every comment
in it to a programmatic rewrite.

`opencode mcp list` inside a session launched this way reports `theater ✓
connected`. The participant id is baked into the command argv, for the reason in
base.py: the MCP SDK drops the parent environment.

Approval modes
--------------
`--auto` is yolo and there is no flag between that and the default. `edits`
therefore degrades to `manual` — the same prompts, no fewer. The alternative was
to emit a `permission` block into the merged config, which was rejected: a key
this adapter has not verified against the running version would take the whole
launch down with it, and a spawn that will not start is worse than a spawn that
asks twice.

Where the output goes
---------------------
    $XDG_DATA_HOME/opencode/opencode-stable.db   (~/.local/share by default)

Four tables matter. `session(id, parent_id, directory, time_created)` locates a
session: `directory` is the *resolved* path, which on macOS means `/private/var`
where the caller says `/var`, so both sides get `Path.resolve()`. `parent_id IS
NULL` excludes sub-agent sessions, which share their parent's directory and
would otherwise be picked up as the newest match. `event(aggregate_id, seq,
type, data)` is the live feed. `message` and `part` hold current state and are
what `history()` reads. Every timestamp in the database is milliseconds.

The database is read `mode=ro` and never through `opencode db`, which takes a
write lock and fails with "database is locked" while a session is running. Note
`immutable=1` is deliberately *not* set: it promises the file will not change,
which is the opposite of what a tail needs.

Event shapes
------------
`message.updated.1` carries `data.info` — id, role, `finish`, `time`. It fires
twice at the end of a message, once with `finish` and once again with
`time.completed`, so finishes are deduped by message id.

`finish == "tool-calls"` ends a *step*, not a turn: a new assistant message
follows immediately. The turn boundary is a finish that is anything else
(`stop`, in every trace sampled). Getting this wrong would resolve a caller's
`await_sessions` at the first tool call with an empty answer.

`message.part.updated.1` carries `data.part` — text, tool, step-start or
step-finish — and `data.time`, the event's own millisecond stamp. Text parts
arrive empty and are then *replaced* by the complete text, not appended to.
Tool parts move pending -> running -> completed, with `state.input` empty until
running and `state.output` set at completed.

Assistant text is therefore buffered and emitted as one event when its message
finishes, rather than streamed. Streaming would put the same reply on the bus
several times over, each a prefix of the last, and hand `_answer_turn` whichever
fragment happened to land last. The cost is that a reply appears at
the end of its step rather than as it is typed; tool activity still streams, so
a long turn is not silent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

from theater.harness.base import (
    APPROVALS,
    SERVER_NAME,
    Event,
    EventKind,
    Harness,
    LaunchPlan,
    clip,
    theater_binary,
    whole,
)
from theater.harness.observation import HarnessObserver
from theater.harness.source import Attachment, Batch, History, Source
from theater.models import BadRequest, Status

logger = logging.getLogger("theater.harness.opencode")

#: Reported by `opencode debug paths` as the data directory's contents. The
#: `-stable` suffix is the release channel: a nightly build writes its own file
#: next to this one, and pointing at the wrong one would find no sessions at
#: all rather than fail loudly.
DB_NAME = "opencode-stable.db"

#: In the footer for exactly as long as a turn is running — present from the
#: instant after Enter. See `is_idle_screen` for why this is the whole test.
WORKING_MARKER = "esc interrupt"

#: The idle footer's right-hand hint. Also present while working, so it is a
#: guard that the footer has drawn at all, not evidence of idleness.
FOOTER_MARKER = "ctrl+p commands"

#: A finish that ends a step but not the turn.
STEP_FINISH = "tool-calls"

#: Events read per poll. A bound, not a target: a session that has been running
#: while the daemon was down could have thousands queued, and reading them in
#: one gulp would block the observer's loop for as long as it takes to parse.
DRAIN_LIMIT = 500


def data_dir() -> Path:
    """Where opencode keeps its state. `$XDG_DATA_HOME` wins if it is set."""
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "opencode"


def _seconds(ms) -> float | None:
    """Milliseconds to a unix epoch float. None for anything else."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    return ms / 1000.0


def _loads(raw) -> dict:
    """A JSON column as a dict. Empty for anything that is not one.

    Rows are read from under a live writer, so a value that does not parse is
    an expected condition rather than a corruption to report.
    """
    if not isinstance(raw, (str, bytes)):
        return {}
    try:
        found = json.loads(raw)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _tool_output(state: dict) -> str:
    """What a finished tool call produced, or what went wrong."""
    output = state.get("output")
    if isinstance(output, str):
        return output
    error = state.get("error")
    if isinstance(error, str):
        return error
    if error is not None:
        return json.dumps(error, default=str)
    return "" if output is None else json.dumps(output, default=str)


class OpenCodeHarness(Harness):
    name = "opencode"
    binary = "opencode"
    #: An open lozenge. The three glyphs already spoken for are `▤` (vibe),
    #: `✻` (claude) and `◉` (codex); this is distinguishable from all of them
    #: at one column and present in any font with Geometric Shapes.
    icon = "\u25c7"
    #: What an agent might call itself at registration. A spelling that does not
    #: normalize is observed as nothing at all, so these are not cosmetic.
    aliases = ("open-code", "open_code", "OpenCode", "opencode-ai")

    def __init__(self, db: Path | None = None):
        #: `db` is where the output lives, which is the observer's business
        #: alone; nothing about launching opencode depends on it.
        self.observer = OpenCodeObserver(db=db)

    # ---- launching ------------------------------------------------------

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
    ) -> LaunchPlan:
        if approval not in APPROVALS:
            raise BadRequest(
                f"approval must be one of {', '.join(APPROVALS)}, got {approval!r}"
            )
        config = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                SERVER_NAME: {
                    "type": "local",
                    "enabled": True,
                    "command": [theater_binary(), "mcp", "--id", participant_id],
                }
            },
        }
        argv = ["opencode"]
        if approval == "yolo":
            argv.append("--auto")
        if prompt:
            # `--prompt` runs it and stays interactive, so a spawned session is
            # working from the start with no keystroke injection.
            argv += ["--prompt", prompt]
        return LaunchPlan(
            argv=argv,
            env={"OPENCODE_CONFIG": str(config_path)},
            files={config_path: json.dumps(config, indent=2)},
        )


class OpenCodeObserver(HarnessObserver):
    """Read one session's rows out of the shared opencode database.

    The adapter that motivated splitting observation off `Harness` in v1.6.
    While the two were one interface this class's four transcript methods —
    `find_transcript`, `session_id`, `parse`, `native_children` — existed only
    to return nothing, because none of those questions has an answer when the
    output is a database rather than a file. Subclassing `HarnessObserver`
    directly instead of `TranscriptObserver` deletes all four.

    `has_transcript` stays True regardless: it means "can be observed by
    reading", not "writes a file", and this adapter reads better than the
    file-backed ones do.

    Known gap, and the reason `native_children` is left at its inherited empty
    default rather than implemented: opencode does have sub-agents and they are
    discoverable — `session.parent_id` points at the parent — but the method is
    keyed by transcript path and there is no path. Surfacing them needs a
    lineage hook on `Source`.
    """

    def __init__(self, db: Path | None = None):
        #: Injectable so tests never touch the real database.
        self.db = db or data_dir() / DB_NAME

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Source:
        return OpenCodeSource(
            self.db, cwd=cwd, session_id=session_id, after=after
        )

    def is_idle_screen(self, capture: str) -> bool:
        """Decided by the absence of `esc interrupt` from the footer.

        Weaker than the other adapters, which match a prompt they can see. The
        composer placeholder (`Ask anything...`) disappears once a conversation
        exists, so there is no positive marker to match after the first turn —
        only the working marker, and its absence. The footer hint guards the
        case that costs the most: a pane that has not drawn yet is blank, and a
        blank capture must not read as a prompt.
        """
        if WORKING_MARKER in capture:
            return False
        return FOOTER_MARKER in capture


class OpenCodeSource(Source):
    """Tail one session's rows in the shared opencode database.

    Holds the connection open for the life of the watcher, and a little state
    the event stream requires: which message each part belongs to, the text
    buffered for a message that has not finished, and how far each tool call
    has got. That state is per session and is dropped on every attach.

    Every query runs under `asyncio.to_thread`, and the connection is opened
    with `check_same_thread=False` because that pool hands out whichever thread
    is free. Concurrent use is not a risk: the observer runs one task per
    participant and awaits each read before the next.
    """

    def __init__(
        self,
        db: Path,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> None:
        self._db = db
        self._cwd = cwd
        #: Set at attach, so a later re-open can use the sharper key.
        self._session_id = session_id
        self._after = after
        self._conn: sqlite3.Connection | None = None
        self._session: str | None = None
        self._cursor = -1
        #: message id -> role, so a text part can be attributed. Filled from
        #: the event stream and, for a message whose creation we skipped at
        #: attach, from the `message` table.
        self._roles: dict[str, str] = {}
        #: message id -> {part id: text}. Insertion-ordered, so joining the
        #: values reassembles a multi-part reply in the order it was written.
        self._text: dict[str, dict[str, str]] = {}
        #: call id -> last status seen, so one tool call yields one TOOL_CALL
        #: and one TOOL_RESULT however many updates it takes to get there.
        self._tools: dict[str, str] = {}
        #: message id -> the time of the last part it produced. The finish
        #: event carries `time.completed` only on its *second* firing, which is
        #: deduped away, so without this a reply would be stamped with when the
        #: model started rather than when it stopped.
        self._stamp: dict[str, float] = {}
        #: Message ids already reported as finished: the finish event fires
        #: twice, and part ids already emitted as user text.
        self._finished: set[str] = set()
        self._said: set[str] = set()

    # ---- Source ---------------------------------------------------------

    async def read(self) -> Batch:
        return await asyncio.to_thread(self._read)

    async def refresh(self) -> Batch:
        return await asyncio.to_thread(self._refresh)

    async def history(self, *, last_n: int) -> History:
        return await asyncio.to_thread(self._history, last_n)

    async def aclose(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                logger.debug("closing the opencode database failed", exc_info=True)

    # ---- synchronous bodies ---------------------------------------------

    def _read(self) -> Batch:
        conn = self._open()
        if conn is None:
            return Batch(waiting=True)
        try:
            if self._session is None:
                found = self._locate(conn, pinned=True)
                return self._attach(conn, found) if found else Batch(waiting=True)
            return self._drain(conn)
        except sqlite3.Error:
            # The database is opened read-only under a live writer; a lock or a
            # transient error is a missed poll, not a dead watcher.
            logger.debug("reading the opencode database failed", exc_info=True)
            return Batch()

    def _refresh(self) -> Batch:
        """Move to the newest session for this directory if there is one.

        The pinned session id is ignored, for the reason `TranscriptSource`
        ignores it: a human can start a fresh session inside the same pane, and
        the id we stored names one that will never grow again.
        """
        conn = self._open()
        if conn is None:
            return Batch()
        try:
            found = self._locate(conn, pinned=False)
            if found is None or found == self._session:
                return Batch()
            logger.info("opencode session changed: %s -> %s", self._session, found)
            return self._attach(conn, found)
        except sqlite3.Error:
            logger.debug("relocating the opencode session failed", exc_info=True)
            return Batch()

    def _history(self, last_n: int) -> History:
        """Rebuild the conversation from `message` and `part`.

        Those tables hold current state, so this never sees the empty-then-full
        intermediates the event stream carries. Read independently of the poll
        cursor: the caller is usually a short-lived source of its own.
        """
        conn = self._open()
        if conn is None:
            return History()
        try:
            sid = self._session or self._locate(conn, pinned=True)
            if sid is None:
                return History()
            parts: dict[str, list[dict]] = {}
            rows = conn.execute(
                "SELECT message_id, data FROM part WHERE session_id = ? "
                "ORDER BY time_created, id",
                (sid,),
            )
            for mid, raw in rows:
                parts.setdefault(mid, []).append(_loads(raw))
            events: list[Event] = []
            rows = conn.execute(
                "SELECT id, data FROM message WHERE session_id = ? "
                "ORDER BY time_created, id",
                (sid,),
            )
            for mid, raw in rows:
                events.extend(self._replay(_loads(raw), parts.get(mid, [])))
        except sqlite3.Error:
            logger.debug("reading opencode history failed", exc_info=True)
            return History()
        if last_n > 0:
            events = events[-last_n:]
        # The stored rows carry no sequence number, so position stands in for
        # one. It is only ever shown back to the caller as `index`.
        events = [replace(e, raw_index=i) for i, e in enumerate(events)]
        return History(location=f"opencode://{sid}", events=events)

    def _replay(self, info: dict, parts: list[dict]) -> list[Event]:
        """One stored message, as events. Text unclipped: this is history."""
        time = info.get("time") if isinstance(info.get("time"), dict) else {}
        ts = _seconds(time.get("completed")) or _seconds(time.get("created"))
        text = "".join(
            p.get("text") or "" for p in parts if p.get("type") == "text"
        )
        if info.get("role") != "assistant":
            return [Event(kind=EventKind.USER, text=whole(text), ts=ts)] if text else []

        out: list[Event] = []
        for part in parts:
            if part.get("type") != "tool":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            name = part.get("tool")
            out.append(Event(kind=EventKind.TOOL_CALL, tool_name=name, ts=ts))
            if state.get("status") in ("completed", "error"):
                out.append(
                    Event(
                        kind=EventKind.TOOL_RESULT,
                        text=whole(_tool_output(state)),
                        tool_name=name,
                        ts=ts,
                    )
                )
        finish = info.get("finish")
        turn_end = bool(finish) and finish != STEP_FINISH
        if text or turn_end:
            # Same rule as the live path: a step that only called tools has
            # already been reported by its tool events.
            out.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    text=whole(text),
                    ts=ts,
                    turn_end=turn_end,
                )
            )
        return out

    # ---- internals ------------------------------------------------------

    def _open(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        if not self._db.exists():
            # Opencode has never run on this machine, or has not written yet.
            return None
        try:
            self._conn = sqlite3.connect(
                f"file:{self._db}?mode=ro", uri=True, check_same_thread=False
            )
        except sqlite3.Error:
            logger.debug("opening %s failed", self._db, exc_info=True)
            return None
        return self._conn

    def _locate(self, conn: sqlite3.Connection, *, pinned: bool) -> str | None:
        if pinned and self._session_id:
            row = conn.execute(
                "SELECT id FROM session WHERE id = ?", (self._session_id,)
            ).fetchone()
            if row is not None:
                return row[0]
        if not self._cwd:
            return None
        want = str(Path(self._cwd).resolve())
        sql = "SELECT id FROM session WHERE directory = ? AND parent_id IS NULL"
        args: list = [want]
        if self._after is not None:
            sql += " AND time_created >= ?"
            args.append(int(self._after * 1000))
        sql += " ORDER BY time_created DESC LIMIT 1"
        row = conn.execute(sql, args).fetchone()
        return row[0] if row is not None else None

    def _attach(self, conn: sqlite3.Connection, sid: str) -> Batch:
        """Point the cursor at the end of a session's events.

        History is skipped rather than replayed, as everywhere else. Status is
        reported explicitly here — it is the one moment the source knows
        something the event stream cannot say, since a session that finished
        before we found it will produce no further events to infer from.
        """
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1), COUNT(*) FROM event WHERE aggregate_id = ?",
            (sid,),
        ).fetchone()
        self._session = self._session_id = sid
        self._cursor = row[0]
        self._roles.clear()
        self._text.clear()
        self._tools.clear()
        self._stamp.clear()
        self._finished.clear()
        self._said.clear()
        return Batch(
            attached=Attachment(
                location=f"opencode://{sid}", session_id=sid, skipped=row[1]
            ),
            status=self._status(conn, sid),
        )

    def _status(self, conn: sqlite3.Connection, sid: str) -> Status:
        """Idle or working, from the newest message.

        A session with no messages is idle: opencode writes the session row
        when the TUI boots, and the prompt's message follows tens of
        milliseconds later. Landing inside that window costs one poll of
        wrongness; calling it WORKING would instead leave a session launched
        with no prompt looking busy until something else moved it.
        """
        row = conn.execute(
            "SELECT data FROM message WHERE session_id = ? "
            "ORDER BY time_created DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if row is None:
            return Status.IDLE
        info = _loads(row[0])
        if info.get("role") != "assistant":
            return Status.WORKING
        time = info.get("time") if isinstance(info.get("time"), dict) else {}
        finish = info.get("finish")
        if finish and finish != STEP_FINISH and time.get("completed"):
            return Status.IDLE
        return Status.WORKING

    def _drain(self, conn: sqlite3.Connection) -> Batch:
        rows = conn.execute(
            "SELECT seq, type, data FROM event WHERE aggregate_id = ? AND seq > ? "
            "ORDER BY seq LIMIT ?",
            (self._session, self._cursor, DRAIN_LIMIT),
        ).fetchall()
        if not rows:
            return Batch()
        events: list[Event] = []
        for seq, kind, raw in rows:
            self._cursor = seq
            events.extend(self._translate(conn, kind, _loads(raw), seq))
        # Rows consumed is progress even when none of them produced an event:
        # session.updated fires throughout a turn, and reading it as silence
        # would let the rescue timer fire in the middle of real work.
        return Batch(events=events, progressed=True)

    def _translate(
        self, conn: sqlite3.Connection, kind: str, payload: dict, seq: int
    ) -> list[Event]:
        if kind == "message.part.updated.1":
            return self._on_part(conn, payload, seq)
        if kind == "message.updated.1":
            return self._on_message(payload, seq)
        # session.created / session.updated: progress, not conversation.
        return []

    def _on_part(
        self, conn: sqlite3.Connection, payload: dict, seq: int
    ) -> list[Event]:
        part = payload.get("part")
        if not isinstance(part, dict):
            return []
        ts = _seconds(payload.get("time"))
        mid = part.get("messageID")
        if ts is not None and isinstance(mid, str):
            self._stamp[mid] = ts
        ptype = part.get("type")
        if ptype == "text":
            return self._on_text(conn, part, ts, seq)
        if ptype == "tool":
            return self._on_tool(part, ts, seq)
        # step-start / step-finish: the shape of the turn, not its content.
        return []

    def _on_text(
        self, conn: sqlite3.Connection, part: dict, ts: float | None, seq: int
    ) -> list[Event]:
        mid = part.get("messageID") or ""
        text = part.get("text") or ""
        if self._role(conn, mid) != "assistant":
            pid = part.get("id") or ""
            if not text or pid in self._said:
                return []
            self._said.add(pid)
            return [
                Event(
                    kind=EventKind.USER,
                    text=clip(text),
                    ts=ts,
                    raw_index=seq,
                )
            ]
        # Replaced, not appended: each update carries the whole part.
        self._text.setdefault(mid, {})[part.get("id") or ""] = text
        return []

    def _on_tool(self, part: dict, ts: float | None, seq: int) -> list[Event]:
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status")
        if not status or status == "pending":
            # Pending carries no arguments yet and may never run at all.
            return []
        call = part.get("callID") or part.get("id") or ""
        name = part.get("tool")
        seen = self._tools.get(call)
        out: list[Event] = []
        if seen is None:
            out.append(
                Event(kind=EventKind.TOOL_CALL, tool_name=name, ts=ts, raw_index=seq)
            )
        done = ("completed", "error")
        if status in done and seen not in done:
            out.append(
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=clip(_tool_output(state)),
                    tool_name=name,
                    ts=ts,
                    raw_index=seq,
                )
            )
        self._tools[call] = status
        return out

    def _on_message(self, payload: dict, seq: int) -> list[Event]:
        info = payload.get("info")
        if not isinstance(info, dict):
            return []
        mid = info.get("id") or ""
        role = info.get("role")
        if isinstance(role, str):
            self._roles[mid] = role
        if role != "assistant":
            return []
        finish = info.get("finish")
        if not finish or mid in self._finished:
            return []
        self._finished.add(mid)
        time = info.get("time") if isinstance(info.get("time"), dict) else {}
        ts = (
            _seconds(time.get("completed"))
            or self._stamp.pop(mid, None)
            or _seconds(time.get("created"))
        )
        text = "".join(self._text.pop(mid, {}).values())
        turn_end = finish != STEP_FINISH
        if not text and not turn_end:
            # A step that only called tools. The tool events already said so;
            # an empty assistant line would just be noise on the bus.
            return []
        return [
            Event(
                kind=EventKind.ASSISTANT,
                text=clip(text),
                ts=ts,
                turn_end=turn_end,
                raw_index=seq,
            )
        ]

    def _role(self, conn: sqlite3.Connection, mid: str) -> str | None:
        """The role of a message, from the stream or from the table.

        The fallback exists for the message whose creation event was skipped at
        attach: its parts keep arriving, and without a role they would be
        attributed to whichever branch guessed.
        """
        role = self._roles.get(mid)
        if role is not None:
            return role
        row = conn.execute("SELECT data FROM message WHERE id = ?", (mid,)).fetchone()
        found = _loads(row[0]).get("role") if row is not None else None
        if isinstance(found, str):
            self._roles[mid] = found
            return found
        return None


#: What the loader looks for. An instance, not the class: see
#: docs/harness-plugins.md. Shipped adapters meet the same contract as anything
#: dropped in $THEATER_HOME/harnesses, which is the point of shipping them here.
HARNESS = OpenCodeHarness()
