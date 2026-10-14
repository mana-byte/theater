"""Claude JSONL decoding and normalized control-event parsing."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Literal

from theater.harness.base import Event, EventKind, EventPath, clipper
from theater.harness.contracts.trajectory import ParsedRecord
from theater.harness.normalization.timing import iso_epoch as _epoch
from theater.harness.normalization.values import decode_json_record

from .constants import _READ_TOOLS, _WRITE_TOOLS
from .seal import FinalAnswerSeal, Sealed, seal_supported
from .usage import _token_usage
from .values import _relativise

logger = logging.getLogger("theater.harness.claude")


class ClaudeParser:
    _seal: FinalAnswerSeal
    #: Whether the last parsed record emitted a seal boundary, so its text is not re-projected.
    _last_seal_emitted: bool = False

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        record = self._decode(line)
        if record is None:
            return []
        return self._parse_decoded(record, index, clip_text=clip_text)

    @staticmethod
    def _decode(line: str) -> dict | None:
        return decode_json_record(line)

    def mutable_tail(self, line: str) -> bool:
        # Claude rewrites its last-wins cost-state line in place, roughly hourly.
        if '"cost-state"' not in line:
            return False
        record = self._decode(line)
        return record is not None and record.get("type") == "cost-state"

    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        record = self._decode(line)
        if record is None:
            return ParsedRecord()
        self._remember_mcp_calls(record)  # type: ignore[attr-defined]
        events = tuple(self._parse_decoded(record, index, clip_text=clip_text))
        return ParsedRecord(
            events=events,
            trajectory=tuple(self._trajectory_facts(record, index)),  # type: ignore[attr-defined]
            # Suppress only the synthesized seal boundary (its text is already projected); the
            # records that trigger it are attachment-like and carry no baseline events of their own.
            trajectory_events=() if self._last_seal_emitted else None,
        )

    def _parse_decoded(self, record: dict, index: int, *, clip_text: bool = True) -> list[Event]:
        ts = _epoch(record.get("timestamp"))
        kind = record.get("type")
        message = record.get("message")
        message = message if isinstance(message, dict) else {}
        self._last_seal_emitted = False
        if (expired := self._seal.observe(record)) is not None and clip_text:
            _report_seal("unavailable", expired)
        deferred = self._emit_deferred(record, index, clip_text=clip_text)
        self._last_seal_emitted = bool(deferred)

        if _is_turn_marker(record):
            boundary = self._seal_boundary(record, ts, index, clip_text=clip_text)
            self._last_seal_emitted = self._last_seal_emitted or bool(boundary)
            return deferred + boundary
        if kind == "assistant":
            return deferred + self._assistant(record, message, ts, index, clip_text=clip_text)
        if kind == "user":
            return deferred + self._user(message, ts, index, clip_text=clip_text)
        if kind == "system" and record.get("level") == "error":
            err = record.get("error")
            text = err if isinstance(err, str) else json.dumps(err, default=str)
            return [
                *deferred,
                Event(
                    kind=EventKind.ERROR,
                    text=clipper(clip_text)(text),
                    raw_text=text,
                    ts=ts,
                    raw_index=index,
                    turn_end=True,
                ),
            ]
        return deferred

    def _emit_deferred(self, record: dict, index: int, *, clip_text: bool) -> list[Event]:
        """Seal a held marker when a later record completes it; a marker or user turn is left
        to the caller's supersede/drop. retry_deferred owns the discontinuity guards."""
        if _is_turn_marker(record):
            return []
        if record.get("type") == "user":
            self._seal.drop_deferred()
            return []
        outcome = self._seal.retry_deferred(record, index)
        if outcome is None:
            return []
        sealed, ts, resolved_index = outcome
        return self._boundary_event(sealed, ts, resolved_index, clip_text=clip_text)

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
        sidechain = record.get("isSidechain") is True
        turn_end = stop is not None and stop != "tool_use" and not sidechain
        final_candidate = turn_end and stop == "end_turn" and seal_supported(record)
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
        elif usage is not None:
            out.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    ts=ts,
                    turn_id=tid,
                    raw_index=index,
                    usage=usage,
                )
            )
        if sidechain:
            return out
        if final_candidate:
            self._seal.expect_marker(tid)
        elif turn_end and out and _carries_answer(out[-1]):
            # Fail closed: a thinking-only or blank fragment is never a boundary.
            out[-1] = replace(out[-1], turn_end=True)
            if clip_text and stop == "end_turn":
                _report_seal("unavailable", "unsupported_version")
        return out

    def _seal_boundary(
        self, record: dict, ts: float | None, index: int, *, clip_text: bool
    ) -> list[Event]:
        """One terminal event when the chain proves the message, else hold the marker for later."""
        if record.get("isSidechain") is True or not seal_supported(record):
            return []
        outcome = self._seal.resolve_or_defer(record, ts, index)
        if isinstance(outcome, Sealed):
            return self._boundary_event(outcome, ts, index, clip_text=clip_text)
        if clip_text and outcome.reason not in ("deferred", "already_sealed"):
            _report_seal("unavailable", outcome.reason)
        return []

    def _boundary_event(
        self, sealed: Sealed, ts: float | None, index: int, *, clip_text: bool
    ) -> list[Event]:
        if clip_text:
            _report_seal("sealed", "empty" if not sealed.raw_text else "text")
        return [
            Event(
                kind=EventKind.ASSISTANT,
                text=clipper(clip_text)(sealed.raw_text),
                raw_text=sealed.raw_text,
                ts=ts,
                turn_end=True,
                turn_id=sealed.message_id,
                raw_index=index,
            )
        ]

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


def _is_turn_marker(record: dict) -> bool:
    return record.get("type") == "system" and record.get("subtype") == "turn_duration"


def _carries_answer(event: Event) -> bool:
    return event.kind is not EventKind.ASSISTANT or bool(event.text or event.raw_text)


def _report_seal(state: str, reason: str) -> None:
    """One low-volume, OTLP-bridged log per live completion: sealed or unavailable, and why."""
    degraded = state != "sealed" and reason != "unsupported_version"
    logger.log(
        logging.WARNING if degraded else logging.INFO,
        "claude completion %s (%s)",
        state,
        reason,
        extra={"theater_completion_seal": state, "theater_completion_reason": reason},
    )
