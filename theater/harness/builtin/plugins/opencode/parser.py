"""OpenCode event and message decoding.

Text parts are mutable replacements, so assistant output waits for its finish.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from theater.harness.base import Event, EventKind, TokenUsage, clip, whole
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.source import Batch

from .constants import DRAIN_LIMIT
from .paths import _paths_from_tool
from .store import event_rows, message_parts, message_role
from .values import (
    _error_detail,
    _has_tool_calls,
    _opencode_usage,
    _seconds,
    _table,
    _tool_output,
    _turn_terminal,
    load_json_object,
)


class OpenCodeParser:
    _cursor: int
    _cwd: str | None
    _finished: set[str]
    _roles: dict[str, str]
    _snapshotted: set[str]
    _said: set[str]
    _session: str | None
    _stamp: dict[str, float]
    _text: dict[str, dict[str, str]]
    _tools: dict[str, str]

    if TYPE_CHECKING:

        def _message_coordinate(
            self, conn: sqlite3.Connection, message_id: object, fallback: int
        ) -> int: ...

        def _trajectory_for_part(
            self, conn: sqlite3.Connection, payload: dict, seq: int, *, raw_index: int
        ) -> list[TrajectoryFact]: ...

        def _trajectory_for_message(
            self,
            conn: sqlite3.Connection,
            payload: dict,
            seq: int,
            *,
            raw_index: int,
            has_tool_calls: bool = False,
        ) -> list[TrajectoryFact]: ...

        def _refresh_mcp_trajectory(self) -> tuple[TrajectoryFact, ...]: ...

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
        # Native's prompt loop keeps running on `tool-calls` and `unknown`,
        # and stops on any other finish — unless the message carries live
        # tool calls, which keep it running too (prompt.ts:1097-1115) — or a
        # stored message error.
        if info.get("error") is None:
            turn_end = _turn_terminal(info, _has_tool_calls(parts))
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
        # A halted turn: the partial content is a step, the failure is the
        # boundary — the same events the live path emits for this stored
        # row, so a turn's accumulated text and tokens agree cold.
        detail = _error_detail(info.get("error"))
        if text:
            out.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    text=whole(text),
                    raw_text=text,
                    ts=ts,
                    turn_end=False,
                    turn_id=info.get("id") or None,
                    usage=_opencode_usage(info),
                )
            )
        out.append(
            Event(
                kind=EventKind.ERROR,
                text=detail,
                raw_text=detail,
                ts=ts,
                turn_end=True,
                turn_id=info.get("id") or None,
                usage=None if text else _opencode_usage(info),
            )
        )
        return out

    def _drain(self, conn: sqlite3.Connection) -> Batch:
        rows = event_rows(conn, self._session, self._cursor, DRAIN_LIMIT)
        if not rows:
            updates = self._refresh_mcp_trajectory()
            return Batch(trajectory=updates, trajectory_events=() if updates else None)
        events: list[Event] = []
        trajectory: list[TrajectoryFact] = []
        for seq, kind, raw in rows:
            self._cursor = seq
            translated, facts = self._translate_with_trajectory(
                conn, kind, load_json_object(raw), seq
            )
            events.extend(translated)
            trajectory.extend(facts)
        trajectory.extend(self._refresh_mcp_trajectory())
        # Rows consumed is progress: session.updated through a turn, else rescue fires mid-turn.
        return Batch(
            events=events,
            progressed=True,
            trajectory=trajectory,
            trajectory_events=(),
        )

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
            # Native classifies a message terminal only when it carries no
            # live tool call (session/prompt.ts:1097-1115), so read the
            # message's current part rows, not just its finish.
            has_tool_calls = (
                isinstance(message_id, str)
                and bool(message_id)
                and _has_tool_calls(
                    load_json_object(row[0]) for row in message_parts(conn, message_id)
                )
            )
            events = self._on_message(payload, seq, has_tool_calls)
            facts = self._trajectory_for_message(
                conn, payload, seq, raw_index=coordinate, has_tool_calls=has_tool_calls
            )
            if (
                isinstance(info, dict)
                and isinstance(message_id, str)
                and _turn_terminal(info, has_tool_calls)
            ):
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

    def _on_message(self, payload: dict, seq: int, has_tool_calls: bool = False) -> list[Event]:
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
        error = info.get("error")
        terminal = _turn_terminal(info, has_tool_calls)
        # A message update matters once it carries a finish or a stored error;
        # plain mid-turn updates are bookkeeping.
        if (not finish and not error) or mid in self._finished:
            return []
        if terminal:
            # Terminal once, however many more message updates repeat it.
            self._finished.add(mid)
            reported = mid in self._snapshotted
        elif mid in self._snapshotted:
            # One snapshot per continuation step. A later terminal update for
            # the same message must still end the turn: a stored error can
            # land after a `tool-calls` finish when a tool call is aborted.
            return []
        else:
            self._snapshotted.add(mid)
            reported = False
        time = _table(info.get("time"))
        ts = (
            _seconds(time.get("completed"))
            or self._stamp.pop(mid, None)
            or _seconds(time.get("created"))
        )
        text = "".join(self._text.get(mid, {}).values())
        # Content and usage already carried by a snapshot are reported once;
        # the idempotency key would deduplicate the usage record anyway,
        # but the event stream should not repeat either.
        usage = None if reported else _opencode_usage(info)
        if not terminal:
            # A continuation step: the content so far, never a turn end.
            if not text and usage is None:
                return []
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=clip(text),
                    raw_text=text,
                    ts=ts,
                    turn_end=False,
                    turn_id=mid or None,
                    raw_index=seq,
                    usage=usage,
                )
            ]
        return self._terminal_events(mid, seq, ts, text, usage, reported, error=error)

    def _terminal_events(
        self,
        mid: str,
        seq: int,
        ts: float | None,
        text: str,
        usage: TokenUsage | None,
        reported: bool,
        *,
        error: object,
    ) -> list[Event]:
        """The events for a message update that ends its turn.

        Content already reported by a continuation snapshot is never
        repeated: the terminal event carries only what is new (the failure
        detail, for a stored error) plus the boundary signal — exactly the
        events history replays for the same stored row. An ERROR event does
        not feed the turn accumulator, so text said by a snapshot is said
        once.
        """
        turn_id = mid or None
        detail = clip(_error_detail(error)) if error is not None else ""
        if error is None:
            if reported:
                # Signal only; the accumulated turn already holds the text.
                return [
                    Event(
                        kind=EventKind.ASSISTANT,
                        text="",
                        raw_text="",
                        ts=ts,
                        turn_end=True,
                        turn_id=turn_id,
                        raw_index=seq,
                    )
                ]
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=clip(text),
                    raw_text=text,
                    ts=ts,
                    turn_end=True,
                    turn_id=turn_id,
                    raw_index=seq,
                    usage=usage,
                )
            ]
        if reported:
            return [
                Event(
                    kind=EventKind.ERROR,
                    text=detail,
                    raw_text=detail,
                    ts=ts,
                    turn_end=True,
                    turn_id=turn_id,
                    raw_index=seq,
                )
            ]
        if text:
            # The partial content is a step; the failure is the boundary.
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=clip(text),
                    raw_text=text,
                    ts=ts,
                    turn_end=False,
                    turn_id=turn_id,
                    raw_index=seq,
                    usage=usage,
                ),
                Event(
                    kind=EventKind.ERROR,
                    text=detail,
                    raw_text=detail,
                    ts=ts,
                    turn_end=True,
                    turn_id=turn_id,
                    raw_index=seq,
                ),
            ]
        # The boundary signal even when the detail renders empty: the turn
        # ended, and an empty ERROR event still settles the accumulator.
        return [
            Event(
                kind=EventKind.ERROR,
                text=detail,
                raw_text=detail,
                ts=ts,
                turn_end=True,
                turn_id=turn_id,
                raw_index=seq,
                usage=usage,
            )
        ]

    def _role(self, conn: sqlite3.Connection, mid: str) -> str | None:
        """Resolve a skipped attachment event from current message state."""
        role = self._roles.get(mid)
        if role is not None:
            return role
        row = message_role(conn, mid)
        found = load_json_object(row[0]).get("role") if row is not None else None
        if isinstance(found, str):
            self._roles[mid] = found
            return found
        return None
