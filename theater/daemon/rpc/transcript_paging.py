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

_CURSOR_PREFIX = "trc3."
_CURSOR_VERSION = 3
_EVENT_DIGEST_BYTES = 32
# Agent reads collapse only events this large; smaller ones stay byte-split (at most ~5 calls).
TRANSCRIPT_COLLAPSE_MIN_BYTES = 64 * 1024
TRANSCRIPT_COLLAPSE_EDGE_BYTES = 6 * 1024
_CURSOR_SECRET = secrets.token_bytes(32)
EventFilter = Callable[[Event], bool]
EventProjection = Callable[[dict[str, object]], dict[str, object]]


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
    tail: str | None = None
    elided_bytes: int = 0
    middle_cursor: str | None = None

    @property
    def reaches_text_start(self) -> bool:
        """Whether this suffix-first chunk includes byte zero of its event."""
        return self.start == 0

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "event_position": self.position,
            "index": self.event.raw_index,
            "role": str(self.event.kind),
            "text": self.text,
            "tool_name": self.event.tool_name,
            "turn_end": self.event.turn_end,
            "turn_terminal": self.event.turn_terminal,
            "text_start_byte": self.start,
            "text_end_byte": self.end,
            "text_total_bytes": self.total,
            "reaches_text_start": self.reaches_text_start,
        }
        if self.tail is not None:
            wire.update(
                tail=self.tail, elided_bytes=self.elided_bytes, middle_cursor=self.middle_cursor
            )
        return wire


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

    def to_wire(
        self, *, target: str, event_projection: EventProjection | None = None
    ) -> dict[str, object]:
        events = [event.to_wire() for event in self.events]
        if event_projection is not None:
            events = [event_projection(event) for event in events]
        return {
            "id": target,
            "events": events,
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
    digest: str


class TranscriptPager:
    """Read bounded source batches and page older material safely."""

    def __init__(
        self,
        source: Source,
        *,
        target: str,
        event_filter: EventFilter,
        max_bytes: int = TRANSCRIPT_READ_RESPONSE_MAX_BYTES,
        event_projection: EventProjection | None = None,
        roles: tuple[str, ...] | None = None,
        collapse_over: int | None = None,
    ) -> None:
        self._source = source
        self._target = target
        self._event_filter = event_filter
        self._max_bytes = max_bytes
        self._event_projection = event_projection
        self._roles = roles
        self._collapse_over = collapse_over

    async def read(self, cursor: str | None = None) -> TranscriptReadPage:
        state = self._decode_cursor(cursor)
        source_page = await self._read_source_page(state, cursor)
        if isinstance(source_page, TranscriptReadPage):
            return source_page
        page, refs = source_page
        start_at, current_end = self._resume_position(page, refs, state)
        if start_at < 0:
            return self._empty_page(page, cursor)
        if state and state["mode"] == "event" and state["stop"] > 0:
            return self._select_middle(page, refs, cursor, start_at, state["end"], state["stop"])
        return self._select_page(page, refs, cursor, start_at, current_end)

    async def _read_source_page(
        self, state: dict[str, Any] | None, cursor: str | None
    ) -> tuple[HistoryPage, tuple[_EventRef, ...]] | TranscriptReadPage:
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
        return page, refs

    def _resume_position(
        self,
        page: HistoryPage,
        refs: tuple[_EventRef, ...],
        state: dict[str, Any] | None,
    ) -> tuple[int, int | None]:
        if state and state["mode"] == "event":
            start_at = next(
                (index for index, ref in enumerate(refs) if ref.position == state["position"]),
                None,
            )
            if start_at is None:
                raise TranscriptCursorError("transcript cursor event is no longer available")
            current_end = state["end"]
            if current_end > len(refs[start_at].encoded) or state["stop"] > current_end:
                raise TranscriptCursorError("transcript cursor text offset is outside its event")
            return start_at, current_end
        return len(refs) - 1, None

    def _empty_page(self, page: HistoryPage, cursor: str | None) -> TranscriptReadPage:
        result = TranscriptReadPage(page, cursor, (), self._older_cursor(page), False)
        if self._wire_size(result) > self._max_bytes:
            raise TranscriptCursorError(
                "transcript response metadata cannot fit within the response budget"
            )
        return result

    def _select_page(
        self,
        page: HistoryPage,
        refs: tuple[_EventRef, ...],
        cursor: str | None,
        start_at: int,
        current_end: int | None,
    ) -> TranscriptReadPage:
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
            full_next_cursor = self._continuation(
                page, refs, ref_index, 0, end, page.snapshot_cursor
            )
            if self._collapses(ref, end):
                collapsed = self._collapsed_chunk(page, cursor, selected, ref, full_next_cursor)
                if collapsed is None:
                    if not selected:
                        raise TranscriptCursorError(
                            "transcript event metadata cannot fit within the response budget"
                        )
                    truncated = True
                    next_cursor = self._continuation(
                        page, refs, ref_index, end, end, page.snapshot_cursor
                    )
                    break
                selected.append(collapsed)
                next_cursor = full_next_cursor
                current_end = None
                continue
            full_candidate = self._chunk(ref, 0, end)
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
        if self._wire_size(result) > self._max_bytes:
            raise TranscriptCursorError("transcript response exceeds the response budget")
        return result

    def _collapses(self, ref: _EventRef, end: int) -> bool:
        limit = self._collapse_over
        return limit is not None and end == len(ref.encoded) and end > limit

    def _collapsed_chunk(
        self,
        page: HistoryPage,
        cursor: str | None,
        selected: list[TranscriptEventChunk],
        ref: _EventRef,
        next_cursor: str | None,
    ) -> TranscriptEventChunk | None:
        """Head and tail of one huge event; middle_cursor pages only the elided range."""
        edge = TRANSCRIPT_COLLAPSE_EDGE_BYTES
        total = len(ref.encoded)
        while True:
            head_end = _utf8_floor(ref.encoded, edge)
            tail_start = _utf8_floor(ref.encoded, total - edge)
            middle = _encode_event_cursor(
                ref, tail_start, self._target, page.snapshot_cursor, self._roles, stop=head_end
            )
            candidate = TranscriptEventChunk(
                ref.event,
                ref.position,
                ref.encoded[:head_end].decode("utf-8"),
                0,
                total,
                total,
                tail=ref.encoded[tail_start:].decode("utf-8"),
                elided_bytes=tail_start - head_end,
                middle_cursor=middle,
            )
            if self._fits(page, cursor, selected, candidate, next_cursor, truncated=False):
                return candidate
            if edge == 0 or selected:
                return None
            edge //= 2

    def _select_middle(
        self,
        page: HistoryPage,
        refs: tuple[_EventRef, ...],
        cursor: str | None,
        start_at: int,
        end: int,
        stop: int,
    ) -> TranscriptReadPage:
        ref = refs[start_at]
        start = self._largest_fitting_start(
            cursor=cursor,
            page=page,
            selected=[],
            ref=ref,
            end=end,
            snapshot=page.snapshot_cursor,
            refs=refs,
            ref_index=start_at,
            stop=stop,
        )
        if start == end:
            raise TranscriptCursorError(
                "transcript event metadata cannot fit within the response budget"
            )
        chunk = self._chunk(ref, start, end)
        next_cursor = self._continuation(
            page, refs, start_at, start, end, page.snapshot_cursor, stop=stop
        )
        return TranscriptReadPage(page, cursor, (chunk,), next_cursor, start > stop)

    def _event_refs(self, page: HistoryPage) -> tuple[_EventRef, ...]:
        refs = []
        for position, event in enumerate(page.transcript_events):
            if not self._event_filter(event):
                continue
            if self._roles is not None and event.kind.value not in self._roles:
                continue
            raw = event.raw_text if event.raw_text is not None else event.text
            encoded = raw.encode("utf-8")
            refs.append(_EventRef(position, event, encoded, hashlib.sha256(encoded).hexdigest()))
        return tuple(refs)

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
        return self._wire_size(result) <= self._max_bytes

    def _wire_size(self, page: TranscriptReadPage) -> int:
        return _encoded_size(
            page.to_wire(target=self._target, event_projection=self._event_projection)
        )

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
        stop: int = 0,
    ) -> int:
        boundaries = _utf8_boundaries(ref.encoded, end, window=self._max_bytes, floor=stop)
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
                self._continuation(page, refs, ref_index, middle, end, snapshot, stop=stop),
                truncated=middle > stop,
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
        *,
        stop: int = 0,
    ) -> str | None:
        if start > stop:
            return _encode_event_cursor(
                refs[ref_index], start, self._target, snapshot, self._roles, stop=stop
            )
        if stop > 0:
            return None
        if ref_index > 0:
            ref = refs[ref_index - 1]
            return _encode_event_cursor(ref, len(ref.encoded), self._target, snapshot, self._roles)
        return self._older_cursor(page)

    def _older_cursor(self, page: HistoryPage) -> str | None:
        if not page.older_cursor:
            return None
        return _encode_older_cursor(page.older_cursor, self._target, self._roles)

    def _decode_cursor(self, cursor: str | None) -> dict[str, Any] | None:
        if cursor is None:
            return None
        payload = self._decode_cursor_payload(cursor)
        self._authenticate_cursor(payload)
        mode = payload.get("mode")
        if mode == "older":
            self._validate_older_cursor(payload)
            return payload
        if mode == "event":
            self._validate_event_cursor(payload)
            return payload
        raise TranscriptCursorError("read_transcript cursor mode is unsupported")

    @staticmethod
    def _decode_cursor_payload(cursor: str) -> dict[str, Any]:
        if isinstance(cursor, str) and cursor.startswith("trc2."):
            raise TranscriptCursorError("read_transcript cursor version is unsupported")
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
        if (
            not isinstance(payload, dict)
            or type(payload.get("v")) is not int
            or payload["v"] != _CURSOR_VERSION
        ):
            raise TranscriptCursorError("read_transcript cursor version is unsupported")
        return payload

    def _authenticate_cursor(self, payload: dict[str, Any]) -> None:
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
        if payload.get("roles") != _roles_wire(self._roles):
            raise TranscriptCursorError("read_transcript cursor was issued for different roles")

    @staticmethod
    def _validate_older_cursor(payload: dict[str, Any]) -> None:
        if set(payload) != {"v", "target", "mode", "source", "roles", "sig"}:
            raise TranscriptCursorError("read_transcript cursor payload is malformed")
        if not _valid_cursor_string(payload.get("source")):
            raise TranscriptCursorError("read_transcript cursor source is malformed")

    @staticmethod
    def _validate_event_cursor(payload: dict[str, Any]) -> None:
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
            "stop",
            "roles",
            "sig",
        }
        if set(payload) != required:
            raise TranscriptCursorError("read_transcript cursor payload is malformed")
        if not _valid_cursor_string(payload.get("snapshot")):
            raise TranscriptCursorError("read_transcript cursor snapshot is malformed")
        if not _valid_event_identity(payload):
            raise TranscriptCursorError("read_transcript cursor event identity is malformed")


def _event_matches(ref: _EventRef, state: dict[str, Any]) -> bool:
    return (
        ref.event.raw_index == state["index"]
        and ref.event.source_offset == state["source_offset"]
        and ref.event.kind.value == state["kind"]
        and ref.event.tool_name == state["tool_name"]
        and ref.digest == state["digest"]
    )


def _utf8_floor(encoded: bytes, offset: int) -> int:
    offset = max(0, min(offset, len(encoded)))
    while 0 < offset < len(encoded) and encoded[offset] & 0xC0 == 0x80:
        offset -= 1
    return offset


def _roles_wire(roles: tuple[str, ...] | None) -> list[str] | None:
    return None if roles is None else list(roles)


def _utf8_boundaries(encoded: bytes, end: int, *, window: int, floor: int = 0) -> tuple[int, ...]:
    if end < 0 or end > len(encoded) or floor > end:
        raise TranscriptCursorError("transcript cursor text offset is outside its event")
    start = max(floor, end - window)
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


def _valid_event_identity(payload: dict[str, Any]) -> bool:
    return not (
        type(payload.get("position")) is not int
        or payload["position"] < 0
        or type(payload.get("index")) is not int
        or type(payload.get("end")) is not int
        or payload["end"] < 0
        or type(payload.get("stop")) is not int
        or payload["stop"] < 0
        or (
            payload.get("source_offset") is not None
            and (type(payload["source_offset"]) is not int or payload["source_offset"] < 0)
        )
        or not isinstance(payload.get("kind"), str)
        or not isinstance(payload.get("tool_name"), (str, type(None)))
        or not isinstance(payload.get("digest"), str)
        or len(payload["digest"]) != _EVENT_DIGEST_BYTES * 2
        or any(char not in "0123456789abcdef" for char in payload["digest"])
    )


def _event_cursor_payload(
    ref: _EventRef,
    end: int,
    target: str,
    snapshot: str | None,
    roles: tuple[str, ...] | None,
    stop: int,
) -> dict[str, object]:
    if snapshot is None:
        raise TranscriptCursorError("transcript source did not return a stable cursor")
    return {
        "v": _CURSOR_VERSION,
        "target": target,
        "mode": "event",
        "snapshot": snapshot,
        "position": ref.position,
        "index": ref.event.raw_index,
        "source_offset": ref.event.source_offset,
        "kind": ref.event.kind.value,
        "tool_name": ref.event.tool_name,
        "digest": ref.digest,
        "end": end,
        "stop": stop,
        "roles": _roles_wire(roles),
    }


def _encode_event_cursor(
    ref: _EventRef,
    end: int,
    target: str,
    snapshot: str | None,
    roles: tuple[str, ...] | None,
    *,
    stop: int = 0,
) -> str:
    return _encode_cursor(_event_cursor_payload(ref, end, target, snapshot, roles, stop))


def _encode_older_cursor(source: str | None, target: str, roles: tuple[str, ...] | None) -> str:
    if source is None:
        raise TranscriptCursorError("transcript source did not return an older cursor")
    return _encode_cursor(
        {
            "v": _CURSOR_VERSION,
            "target": target,
            "mode": "older",
            "source": source,
            "roles": _roles_wire(roles),
        }
    )


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
    "TRANSCRIPT_COLLAPSE_MIN_BYTES",
    "TranscriptCursorError",
    "TranscriptEventChunk",
    "TranscriptPager",
    "TranscriptReadPage",
]
