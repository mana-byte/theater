"""OpenCode event and message decoding.

Text parts are mutable replacements, so assistant output waits for its finish.
"""

# mypy: disable-error-code="attr-defined,has-type"

from __future__ import annotations

import sqlite3

from theater.harness.base import Event, EventKind, clip, whole
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.source import Batch

from .constants import DRAIN_LIMIT, STEP_FINISH
from .store import event_rows, message_role
from .trajectory import _loads, _opencode_usage, _paths_from_tool, _seconds, _table, _tool_output


class OpenCodeParser:
    def _replay(self, info: dict, parts: list[dict]) -> list[Event]:
        """One stored message, as events. Text unclipped: this is history."""
        time = _table(info.get("time"))
        ts = _seconds(time.get("completed")) or _seconds(time.get("created"))
        text = "".join(p.get("text") or "" for p in parts if p.get("type") == "text")
        if info.get("role") != "assistant":
            return (
                [Event(kind=EventKind.USER, text=whole(text), raw_text=text, ts=ts)] if text else []
            )

        out: list[Event] = []
        for part in parts:
            if part.get("type") != "tool":
                continue
            state = _table(part.get("state"))
            name = part.get("tool")
            paths = _paths_from_tool(name or "", state, self._cwd)
            out.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=name,
                    ts=ts,
                    paths=paths,
                )
            )
            if state.get("status") in ("completed", "error"):
                raw = _tool_output(state)
                out.append(
                    Event(
                        kind=EventKind.TOOL_RESULT,
                        text=whole(raw),
                        raw_text=raw,
                        tool_name=name,
                        ts=ts,
                    )
                )
        finish = info.get("finish")
        turn_end = bool(finish) and finish != STEP_FINISH
        usage = _opencode_usage(info)
        if text or turn_end or usage is not None:
            out.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    text=whole(text),
                    raw_text=text,
                    ts=ts,
                    turn_end=turn_end,
                    turn_id=info.get("id") or None,
                    usage=usage,
                )
            )
        return out

    def _drain(self, conn: sqlite3.Connection) -> Batch:
        rows = event_rows(conn, self._session, self._cursor, DRAIN_LIMIT)
        if not rows:
            return Batch()
        events: list[Event] = []
        trajectory: list[TrajectoryFact] = []
        for seq, kind, raw in rows:
            self._cursor = seq
            translated, facts = self._translate_with_trajectory(conn, kind, _loads(raw), seq)
            events.extend(translated)
            trajectory.extend(facts)
        # Rows consumed is progress: session.updated through a turn, else rescue fires mid-turn.
        return Batch(
            events=events,
            progressed=True,
            trajectory=trajectory,
            trajectory_events=(),
        )

    def _translate(
        self, conn: sqlite3.Connection, kind: str, payload: dict, seq: int
    ) -> list[Event]:
        return self._translate_with_trajectory(conn, kind, payload, seq)[0]

    def _translate_with_trajectory(
        self, conn: sqlite3.Connection, kind: str, payload: dict, seq: int
    ) -> tuple[list[Event], list[TrajectoryFact]]:
        if kind == "message.part.updated.1":
            part = payload.get("part")
            message_id = part.get("messageID") if isinstance(part, dict) else None
            coordinate = self._message_coordinate(conn, message_id, seq)
            events = self._on_part(conn, payload, seq)
            return events, self._trajectory_for_part(conn, payload, seq, raw_index=coordinate)
        if kind == "message.updated.1":
            info = payload.get("info")
            message_id = info.get("id") if isinstance(info, dict) else None
            coordinate = self._message_coordinate(conn, message_id, seq)
            events = self._on_message(payload, seq)
            facts = self._trajectory_for_message(conn, payload, seq, raw_index=coordinate)
            if isinstance(info, dict):
                finish = info.get("finish")
                if finish and finish != STEP_FINISH and isinstance(message_id, str):
                    self._text.pop(message_id, None)
            return events, facts
        # session.created / session.updated: progress, not conversation.
        return [], []

    def _on_part(self, conn: sqlite3.Connection, payload: dict, seq: int) -> list[Event]:
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
                    raw_text=text,
                    ts=ts,
                    raw_index=seq,
                )
            ]
        # Replaced, not appended: each update carries the whole part.
        self._text.setdefault(mid, {})[part.get("id") or ""] = text
        return []

    def _on_tool(self, part: dict, ts: float | None, seq: int) -> list[Event]:
        state = _table(part.get("state"))
        status = state.get("status")
        if not status or status == "pending":
            # Pending may never run at all.
            return []
        call = part.get("callID") or part.get("id") or ""
        name = part.get("tool")
        seen = self._tools.get(call)
        out: list[Event] = []
        if seen is None:
            # `running` is the first status that carries `state.input`, so paths are available here.
            paths = _paths_from_tool(name or "", state, self._cwd)
            out.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=name,
                    ts=ts,
                    raw_index=seq,
                    paths=paths,
                )
            )
        done = ("completed", "error")
        if status in done and seen not in done:
            raw = _tool_output(state)
            out.append(
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=clip(raw),
                    raw_text=raw,
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
        time = _table(info.get("time"))
        ts = (
            _seconds(time.get("completed"))
            or self._stamp.pop(mid, None)
            or _seconds(time.get("created"))
        )
        text = "".join(self._text.get(mid, {}).values())
        turn_end = finish != STEP_FINISH
        usage = _opencode_usage(info)
        if not text and not turn_end and usage is None:
            return []
        return [
            Event(
                kind=EventKind.ASSISTANT,
                text=clip(text),
                raw_text=text,
                ts=ts,
                turn_end=turn_end,
                turn_id=mid or None,
                raw_index=seq,
                usage=usage,
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
        row = message_role(conn, mid)
        found = _loads(row[0]).get("role") if row is not None else None
        if isinstance(found, str):
            self._roles[mid] = found
            return found
        return None
