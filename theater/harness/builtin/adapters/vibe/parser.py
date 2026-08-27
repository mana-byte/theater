"""Vibe record decoding and turn state."""

from __future__ import annotations

from typing import TYPE_CHECKING, BinaryIO

from theater.constants.trajectory import TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES
from theater.harness.base import Event, EventKind, clipper
from theater.harness.contracts.trajectory import ParsedRecord
from theater.harness.normalization.values import decode_json_record

from .trajectory import _extract_paths, _vibe_message_id

if TYPE_CHECKING:
    from theater.harness.contracts.trajectory import TrajectoryFact


class VibeParserMixin:
    _active_turn_id: str | None
    _last_turn_id: str | None
    _cwd: str | None

    if TYPE_CHECKING:

        def _facts_for_record(
            self, record: dict, index: int, *, turn_id: str | None
        ) -> list[TrajectoryFact]: ...

    @property
    def current_turn_id(self) -> str | None:
        return self._active_turn_id or self._last_turn_id

    def _reset_turn_context(self) -> None:
        self._active_turn_id = None
        self._last_turn_id = None

    def _advance_turn(self, record: dict, index: int) -> tuple[str | None, bool]:
        explicit: str | None = next(
            (
                value
                for key in ("turn_id", "turnId")
                if isinstance((value := record.get(key)), str) and value
            ),
            None,
        )
        role = record.get("role")
        turn_id: str | None
        if role == "user":
            if explicit is not None:
                turn_id = explicit
            elif record.get("injected") is True and self.current_turn_id is not None:
                turn_id = self.current_turn_id
            else:
                turn_id = _vibe_message_id(record) or f"vibe-turn:{max(0, index)}"
            self._active_turn_id = turn_id
        else:
            turn_id = explicit or self.current_turn_id
            if explicit is not None:
                self._active_turn_id = explicit

        turn_end = role == "assistant" and not (record.get("tool_calls") or [])
        if turn_end:
            self._last_turn_id = turn_id
            self._active_turn_id = None
        return turn_id, turn_end

    def _seed_history_context(self, fh: BinaryIO, start: int) -> None:
        self._reset_turn_context()
        scan_start = max(0, start - TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES)
        at_boundary = scan_start == 0
        if scan_start:
            fh.seek(scan_start - 1)
            at_boundary = fh.read(1) == b"\n"
        fh.seek(scan_start)
        context = fh.read(start - scan_start)
        if not at_boundary:
            _, separator, context = context.partition(b"\n")
            if not separator:
                return
        for index, raw in enumerate(context.splitlines()):
            record = decode_json_record(raw.decode("utf-8", errors="replace"))
            if isinstance(record, dict):
                self._advance_turn(record, index)

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        return list(self.parse_record(line, index, clip_text=clip_text).events)

    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        record = decode_json_record(line)
        if record is None:
            return ParsedRecord()
        turn_id, turn_end = self._advance_turn(record, index)
        return ParsedRecord(
            events=tuple(
                self._events_for_record(
                    record,
                    index,
                    clip_text=clip_text,
                    turn_id=turn_id,
                    turn_end=turn_end,
                )
            ),
            trajectory=tuple(self._facts_for_record(record, index, turn_id=turn_id)),
            trajectory_events=(),
        )

    def _events_for_record(
        self,
        record: dict,
        index: int,
        *,
        clip_text: bool = True,
        turn_id: str | None,
        turn_end: bool,
    ) -> list[Event]:
        _clip = clipper(clip_text)

        role = record.get("role")
        if role == "user":
            raw = record.get("content") if isinstance(record.get("content"), str) else ""
            return [
                Event(
                    kind=EventKind.USER,
                    text=_clip(raw),
                    raw_text=raw,
                    turn_id=turn_id,
                    raw_index=index,
                )
            ]
        if role == "tool":
            raw = record.get("content") if isinstance(record.get("content"), str) else ""
            return [
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=_clip(raw),
                    raw_text=raw,
                    tool_name=record.get("name"),
                    turn_id=turn_id,
                    raw_index=index,
                )
            ]
        if role != "assistant":
            return []

        calls = record.get("tool_calls") or []
        out: list[Event] = []
        content = record.get("content")
        if content:
            raw = content if isinstance(content, str) else ""
            out.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    text=_clip(raw),
                    raw_text=raw,
                    turn_id=turn_id,
                    raw_index=index,
                )
            )
        for call in calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            fn_name = fn.get("name") if isinstance(fn, dict) else None
            fn_args = fn.get("arguments") if isinstance(fn, dict) else None
            paths = _extract_paths(fn_name, fn_args, self._cwd)
            out.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=fn_name,
                    turn_id=turn_id,
                    raw_index=index,
                    paths=paths,
                )
            )
        if not turn_end:
            return out
        if out:
            last = out[-1]
            out[-1] = Event(
                kind=last.kind,
                text=last.text,
                raw_text=last.raw_text,
                tool_name=last.tool_name,
                ts=last.ts,
                turn_end=True,
                turn_id=turn_id,
                raw_index=last.raw_index,
                paths=last.paths,
            )
        else:
            out.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    turn_end=True,
                    turn_id=turn_id,
                    raw_index=index,
                )
            )
        return out
