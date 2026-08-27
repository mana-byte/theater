"""Claude JSONL decoding and normalized control-event parsing."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Literal

from theater.harness.base import Event, EventKind, EventPath, clipper
from theater.harness.contracts.trajectory import ParsedRecord

from .constants import _READ_TOOLS, _WRITE_TOOLS
from .timing import _epoch
from .usage import _token_usage
from .values import _relativise


class ClaudeParser:
    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        record = self._decode(line)
        if record is None:
            return []
        return self._parse_decoded(record, index, clip_text=clip_text)

    @staticmethod
    def _decode(line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None
        try:
            record = json.loads(line)
        except ValueError:
            return None
        return record if isinstance(record, dict) else None

    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        record = self._decode(line)
        if record is None:
            return ParsedRecord()
        self._remember_mcp_calls(record)  # type: ignore[attr-defined]
        return ParsedRecord(
            events=tuple(self._parse_decoded(record, index, clip_text=clip_text)),
            trajectory=tuple(self._trajectory_facts(record, index)),  # type: ignore[attr-defined]
        )

    def _parse_decoded(self, record: dict, index: int, *, clip_text: bool = True) -> list[Event]:
        ts = _epoch(record.get("timestamp"))
        kind = record.get("type")
        message = record.get("message")
        message = message if isinstance(message, dict) else {}

        if kind == "assistant":
            return self._assistant(record, message, ts, index, clip_text=clip_text)
        if kind == "user":
            return self._user(message, ts, index, clip_text=clip_text)
        if kind == "system" and record.get("level") == "error":
            err = record.get("error")
            text = err if isinstance(err, str) else json.dumps(err, default=str)
            return [
                Event(
                    kind=EventKind.ERROR,
                    text=clipper(clip_text)(text),
                    raw_text=text,
                    ts=ts,
                    raw_index=index,
                    turn_end=True,
                )
            ]
        return []

    def _assistant(
        self,
        record: dict,
        message: dict,
        ts: float | None,
        index: int,
        *,
        clip_text: bool = True,
    ) -> list[Event]:
        _clip = clipper(clip_text)
        stop = message.get("stop_reason")
        turn_end = stop is not None and stop != "tool_use"
        tid = message.get("id") or record.get("requestId")
        tid = tid if isinstance(tid, str) and tid else None
        cwd = record.get("cwd")
        cwd = cwd if isinstance(cwd, str) and cwd else None
        usage = _token_usage(message, record)
        out: list[Event] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                raw = block.get("text") if isinstance(block.get("text"), str) else ""
                out.append(
                    Event(
                        kind=EventKind.ASSISTANT,
                        text=_clip(raw),
                        raw_text=raw,
                        ts=ts,
                        turn_id=tid,
                        raw_index=index,
                    )
                )
            elif btype == "tool_use":
                out.append(
                    Event(
                        kind=EventKind.TOOL_CALL,
                        tool_name=block.get("name"),
                        ts=ts,
                        turn_id=tid,
                        raw_index=index,
                        paths=self._tool_paths(block.get("name"), block.get("input"), cwd),
                    )
                )
        if usage is not None and out:
            out[-1] = replace(out[-1], usage=usage)
        elif usage is not None and not out and not turn_end:
            out.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    ts=ts,
                    turn_id=tid,
                    raw_index=index,
                    usage=usage,
                )
            )
        if turn_end:
            if out:
                out[-1] = replace(out[-1], turn_end=True)
            else:
                out.append(
                    Event(
                        kind=EventKind.ASSISTANT,
                        ts=ts,
                        turn_end=True,
                        turn_id=tid,
                        raw_index=index,
                        usage=usage,
                    )
                )
        return out

    def _tool_paths(
        self, name: str | None, tool_input: object, cwd: str | None
    ) -> tuple[EventPath, ...]:
        if not isinstance(tool_input, dict):
            return ()
        key = _WRITE_TOOLS.get(name or "") or _READ_TOOLS.get(name or "")
        if key is None:
            return ()
        raw = tool_input.get(key)
        if not isinstance(raw, str) or not raw:
            return ()
        mode: Literal["read", "write"] = "write" if name in _WRITE_TOOLS else "read"
        rel = _relativise(raw, cwd)
        return (EventPath(path=rel, mode=mode),) if rel is not None else ()

    def _user(
        self, message: dict, ts: float | None, index: int, *, clip_text: bool = True
    ) -> list[Event]:
        _clip = clipper(clip_text)
        content = message.get("content")
        if isinstance(content, str):
            return [
                Event(
                    kind=EventKind.USER,
                    text=_clip(content),
                    raw_text=content,
                    ts=ts,
                    raw_index=index,
                )
            ]
        out: list[Event] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                body = block.get("content")
                raw = body if isinstance(body, str) else json.dumps(body, default=str)
                out.append(
                    Event(
                        kind=EventKind.TOOL_RESULT,
                        text=_clip(raw),
                        raw_text=raw,
                        ts=ts,
                        raw_index=index,
                    )
                )
            elif block.get("type") == "text":
                text = block.get("text")
                raw = text if isinstance(text, str) else ""
                out.append(
                    Event(
                        kind=EventKind.USER,
                        text=_clip(raw),
                        raw_text=raw,
                        ts=ts,
                        raw_index=index,
                    )
                )
        return out
