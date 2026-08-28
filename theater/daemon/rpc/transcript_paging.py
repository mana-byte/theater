"""Daemon-owned paging for agent-facing transcript reads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from theater.constants.daemon import (
    TRANSCRIPT_READ_EMPTY_PAGE_SCAN_LIMIT,
    TRANSCRIPT_READ_RESPONSE_MAX_BYTES,
    TRANSCRIPT_READ_SOURCE_PAGE_LIMIT,
)
from theater.constants.trajectory import TRAJECTORY_CURSOR_MAX_BYTES
from theater.harness.contracts.events import Event
from theater.harness.contracts.source import HistoryPage, Source

_CURSOR_PREFIX = "trc2."
_EVENT_DIGEST_BYTES = 32
_CURSOR_SECRET = secrets.token_bytes(32)
EventFilter = Callable[[Event], bool]


class TranscriptCursorError(ValueError):
    """A transcript cursor is malformed, stale, or belongs to another source."""

    def __init__(self, message: str) -> None:
        super().__init__(f"{message}; omit cursor to restart from newest")


@dataclass(frozen=True, slots=True)
class TranscriptEventChunk:
    event: Event
    position: int
    text: str
    start: int
    end: int
    total: int

    @property
    def reaches_text_start(self) -> bool:
        """Whether this suffix-first chunk includes byte zero of its event."""
        return self.start == 0

    def to_wire(self) -> dict[str, object]:
        return {
            "event_position": self.position,
            "index": self.event.raw_index,
            "role": str(self.event.kind),
            "text": self.text,
            "tool_name": self.event.tool_name,
            "turn_end": self.event.turn_end,
            "text_start_byte": self.start,
            "text_end_byte": self.end,
            "text_total_bytes": self.total,
            "reaches_text_start": self.reaches_text_start,
        }


@dataclass(frozen=True, slots=True)
class TranscriptReadPage:
    """One response; truncated means the response budget cut this source page."""

    history: HistoryPage
    cursor: str | None
    events: tuple[TranscriptEventChunk, ...]
    next_cursor: str | None
    truncated: bool

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    def to_wire(self, *, target: str) -> dict[str, object]:
        return {
            "id": target,
            "events": [event.to_wire() for event in self.events],
            "path": self.history.location,
            "cursor": self.cursor,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class _EventRef:
    position: int
    event: Event
    encoded: bytes


class TranscriptPager:
    """Read bounded source batches and page older material safely."""

    def __init__(
        self,
        source: Source,
        *,
        target: str,
        event_filter: EventFilter,
        max_bytes: int = TRANSCRIPT_READ_RESPONSE_MAX_BYTES,
    ) -> None:
        self._source = source
        self._target = target
        self._event_filter = event_filter
        self._max_bytes = max_bytes

    async def read(self, cursor: str | None = None) -> TranscriptReadPage:  # noqa: PLR0912, PLR0915
        state = self._decode_cursor(cursor)
        before = state["source"] if state and state["mode"] == "older" else None
        snapshot = state["snapshot"] if state and state["mode"] == "event" else None
        page_before = before
        empty_pages = 0
        while True:
            page = await self._source.history_page(
                before=page_before,
                snapshot=snapshot,
                limit=TRANSCRIPT_READ_SOURCE_PAGE_LIMIT,
                include_full_text=True,
            )
            if page.error_code is not None:
                if cursor is not None and page.error_code == "history_cursor_invalid":
                    raise TranscriptCursorError(
                        page.error or "transcript source rejected the continuation cursor"
                    )
                return TranscriptReadPage(page, cursor, (), None, False)
            if state and state["mode"] == "event":
                self._validate_snapshot(page, state)
            refs = self._event_refs(page)
            if refs or not page.older_cursor:
                break
            empty_pages += 1
            if empty_pages >= TRANSCRIPT_READ_EMPTY_PAGE_SCAN_LIMIT:
                break
            page_before = page.older_cursor

        if state and state["mode"] == "event":
            start_at = next(
                (index for index, ref in enumerate(refs) if ref.position == state["position"]),
                None,
            )
            if start_at is None:
                raise TranscriptCursorError("transcript cursor event is no longer available")
            current_end = state["end"]
            if current_end > len(refs[start_at].encoded):
                raise TranscriptCursorError("transcript cursor text offset is outside its event")
        else:
            start_at = len(refs) - 1
            current_end = None

        if start_at < 0:
            empty_next_cursor = self._older_cursor(page)
            result = TranscriptReadPage(page, cursor, (), empty_next_cursor, False)
            if _encoded_size(result.to_wire(target=self._target)) > self._max_bytes:
                raise TranscriptCursorError(
                    "transcript response metadata cannot fit within the response budget"
                )
            return result

        selected: list[TranscriptEventChunk] = []
        truncated = False
        next_cursor: str | None = self._older_cursor(page)
        for ref_index in range(start_at, -1, -1):
            ref = refs[ref_index]
            end = (
                current_end
                if ref_index == start_at and current_end is not None
                else len(ref.encoded)
            )
            full_candidate = self._chunk(ref, 0, end)
            full_next_cursor = self._continuation(
                page, refs, ref_index, 0, end, page.snapshot_cursor
            )
            if self._fits(
                page, cursor, selected, full_candidate, full_next_cursor, truncated=False
            ):
                selected.append(full_candidate)
                next_cursor = full_next_cursor
                current_end = None
                continue

            start = self._largest_fitting_start(
                cursor=cursor,
                page=page,
                selected=selected,
                ref=ref,
                end=end,
                snapshot=page.snapshot_cursor,
                refs=refs,
                ref_index=ref_index,
            )
            if start == end:
                if selected:
                    truncated = True
                    next_cursor = self._continuation(
                        page, refs, ref_index, end, end, page.snapshot_cursor
                    )
                    break
                raise TranscriptCursorError(
                    "transcript event metadata cannot fit within the response budget"
                )
            chunk = self._chunk(ref, start, end)
            next_cursor = self._continuation(
                page, refs, ref_index, start, end, page.snapshot_cursor
            )
            selected.append(chunk)
            truncated = True
            break
        result = TranscriptReadPage(page, cursor, tuple(reversed(selected)), next_cursor, truncated)
        if _encoded_size(result.to_wire(target=self._target)) > self._max_bytes:
            raise TranscriptCursorError("transcript response exceeds the response budget")
        return result

    def _event_refs(self, page: HistoryPage) -> tuple[_EventRef, ...]:
        return tuple(
            _EventRef(
                position=position,
                event=event,
                encoded=(event.raw_text if event.raw_text is not None else event.text).encode(
                    "utf-8"
                ),
            )
            for position, event in enumerate(page.transcript_events)
            if self._event_filter(event)
        )

    def _validate_snapshot(self, page: HistoryPage, state: dict[str, Any]) -> None:
        if page.snapshot_cursor != state["snapshot"]:
            raise TranscriptCursorError("transcript changed since this cursor was issued")
        refs = self._event_refs(page)
        ref = next((item for item in refs if item.position == state["position"]), None)
        if ref is None:
            raise TranscriptCursorError("transcript cursor event is no longer available")
        if not _event_matches(ref, state):
            raise TranscriptCursorError("transcript cursor event changed")
        if state["end"] > len(ref.encoded):
            raise TranscriptCursorError("transcript cursor text offset is outside its event")
        try:
            ref.encoded[: state["end"]].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TranscriptCursorError("transcript cursor splits a UTF-8 character") from exc

    def _chunk(self, ref: _EventRef, start: int, end: int) -> TranscriptEventChunk:
        try:
            text = ref.encoded[start:end].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TranscriptCursorError("transcript cursor splits a UTF-8 character") from exc
        return TranscriptEventChunk(ref.event, ref.position, text, start, end, len(ref.encoded))

    def _fits(
        self,
        page: HistoryPage,
        cursor: str | None,
        selected: list[TranscriptEventChunk],
        candidate: TranscriptEventChunk,
        next_cursor: str | None,
        *,
        truncated: bool,
    ) -> bool:
        events = tuple(reversed((*selected, candidate)))
        result = TranscriptReadPage(page, cursor, events, next_cursor, truncated)
        return _encoded_size(result.to_wire(target=self._target)) <= self._max_bytes

    def _largest_fitting_start(
        self,
        *,
        cursor: str | None,
        page: HistoryPage,
        selected: list[TranscriptEventChunk],
        ref: _EventRef,
        end: int,
        snapshot: str | None,
        refs: tuple[_EventRef, ...],
        ref_index: int,
    ) -> int:
        boundaries = _utf8_boundaries(ref.encoded, end, window=self._max_bytes)
        low, high, best = 0, len(boundaries) - 1, end
        while low <= high:
            middle_index = (low + high) // 2
            middle = boundaries[middle_index]
            candidate = self._chunk(ref, middle, end)
            if self._fits(
                page,
                cursor,
                selected,
                candidate,
                self._continuation(page, refs, ref_index, middle, end, snapshot),
                truncated=middle > 0,
            ):
                best = middle
                high = middle_index - 1
            else:
                low = middle_index + 1
        return best

    def _continuation(
        self,
        page: HistoryPage,
        refs: tuple[_EventRef, ...],
        ref_index: int,
        start: int,
        end: int,
        snapshot: str | None,
    ) -> str | None:
        if start > 0:
            return _encode_event_cursor(refs[ref_index], start, self._target, snapshot)
        if ref_index > 0:
            ref = refs[ref_index - 1]
            return _encode_event_cursor(ref, len(ref.encoded), self._target, snapshot)
        return self._older_cursor(page)

    def _older_cursor(self, page: HistoryPage) -> str | None:
        return _encode_older_cursor(page.older_cursor, self._target) if page.older_cursor else None

    def _decode_cursor(self, cursor: str | None) -> dict[str, Any] | None:  # noqa: PLR0912
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor.startswith(_CURSOR_PREFIX):
            raise TranscriptCursorError(
                "read_transcript cursor is malformed; use only next_cursor from Theater"
            )
        try:
            if len(cursor.encode("utf-8")) > TRAJECTORY_CURSOR_MAX_BYTES:
                raise TranscriptCursorError("read_transcript cursor is too large")
        except UnicodeEncodeError as exc:
            raise TranscriptCursorError("read_transcript cursor is malformed") from exc
        encoded = cursor.removeprefix(_CURSOR_PREFIX)
        if not encoded or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for char in encoded
        ):
            raise TranscriptCursorError("read_transcript cursor is malformed")
        try:
            raw = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
            raise TranscriptCursorError("read_transcript cursor is malformed") from exc
        if not isinstance(payload, dict) or type(payload.get("v")) is not int or payload["v"] != 2:
            raise TranscriptCursorError("read_transcript cursor version is unsupported")
        signature = payload.get("sig")
        unsigned = dict(payload)
        unsigned.pop("sig", None)
        if not isinstance(signature, str) or not hmac.compare_digest(
            signature, _cursor_signature(unsigned)
        ):
            raise TranscriptCursorError(
                "read_transcript cursor signature is invalid; use only next_cursor from Theater"
            )
        if payload.get("target") != self._target:
            raise TranscriptCursorError("read_transcript cursor belongs to another participant")
        mode = payload.get("mode")
        if mode == "older":
            if set(payload) != {"v", "target", "mode", "source", "sig"}:
                raise TranscriptCursorError("read_transcript cursor payload is malformed")
            source = payload.get("source")
            if not _valid_cursor_string(source):
                raise TranscriptCursorError("read_transcript cursor source is malformed")
            return payload
        if mode == "event":
            required = {
                "v",
                "target",
                "mode",
                "snapshot",
                "position",
                "index",
                "source_offset",
                "kind",
                "tool_name",
                "digest",
                "end",
                "sig",
            }
            if set(payload) != required:
                raise TranscriptCursorError("read_transcript cursor payload is malformed")
            if not _valid_cursor_string(payload.get("snapshot")):
                raise TranscriptCursorError("read_transcript cursor snapshot is malformed")
            if (
                type(payload.get("position")) is not int
                or payload["position"] < 0
                or type(payload.get("index")) is not int
                or type(payload.get("end")) is not int
                or payload["end"] < 0
                or (
                    payload.get("source_offset") is not None
                    and (type(payload["source_offset"]) is not int or payload["source_offset"] < 0)
                )
                or not isinstance(payload.get("kind"), str)
                or not isinstance(payload.get("tool_name"), (str, type(None)))
                or not isinstance(payload.get("digest"), str)
                or len(payload["digest"]) != _EVENT_DIGEST_BYTES * 2
                or any(char not in "0123456789abcdef" for char in payload["digest"])
            ):
                raise TranscriptCursorError("read_transcript cursor event identity is malformed")
            return payload
        raise TranscriptCursorError("read_transcript cursor mode is unsupported")


def _event_matches(ref: _EventRef, state: dict[str, Any]) -> bool:
    return (
        ref.event.raw_index == state["index"]
        and ref.event.source_offset == state["source_offset"]
        and ref.event.kind.value == state["kind"]
        and ref.event.tool_name == state["tool_name"]
        and hashlib.sha256(ref.encoded).hexdigest() == state["digest"]
    )


def _utf8_boundaries(encoded: bytes, end: int, *, window: int) -> tuple[int, ...]:
    if end < 0 or end > len(encoded):
        raise TranscriptCursorError("transcript cursor text offset is outside its event")
    start = max(0, end - window)
    while start < end and encoded[start] & 0xC0 == 0x80:
        start += 1
    boundaries = [start]
    boundaries.extend(
        offset
        for offset in range(start + 1, end + 1)
        if offset == end or encoded[offset] & 0xC0 != 0x80
    )
    if boundaries[-1] != end:
        raise TranscriptCursorError("transcript cursor splits a UTF-8 character")
    return tuple(boundaries)


def _valid_cursor_string(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= TRAJECTORY_CURSOR_MAX_BYTES
    except UnicodeEncodeError:
        return False


def _event_cursor_payload(
    ref: _EventRef, end: int, target: str, snapshot: str | None
) -> dict[str, object]:
    if snapshot is None:
        raise TranscriptCursorError("transcript source did not return a stable cursor")
    return {
        "v": 2,
        "target": target,
        "mode": "event",
        "snapshot": snapshot,
        "position": ref.position,
        "index": ref.event.raw_index,
        "source_offset": ref.event.source_offset,
        "kind": ref.event.kind.value,
        "tool_name": ref.event.tool_name,
        "digest": hashlib.sha256(ref.encoded).hexdigest(),
        "end": end,
    }


def _encode_event_cursor(ref: _EventRef, end: int, target: str, snapshot: str | None) -> str:
    return _encode_cursor(_event_cursor_payload(ref, end, target, snapshot))


def _encode_older_cursor(source: str | None, target: str) -> str:
    if source is None:
        raise TranscriptCursorError("transcript source did not return an older cursor")
    return _encode_cursor({"v": 2, "target": target, "mode": "older", "source": source})


def _encode_cursor(payload: dict[str, object]) -> str:
    signed = {**payload, "sig": _cursor_signature(payload)}
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(signed, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    cursor = _CURSOR_PREFIX + encoded
    if len(cursor.encode("utf-8")) > TRAJECTORY_CURSOR_MAX_BYTES:
        raise TranscriptCursorError("read_transcript continuation cursor is too large")
    return cursor


def _cursor_signature(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(_CURSOR_SECRET, encoded, hashlib.sha256).hexdigest()


def _encoded_size(payload: dict[str, object]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


__all__ = [
    "TranscriptCursorError",
    "TranscriptEventChunk",
    "TranscriptPager",
    "TranscriptReadPage",
]
