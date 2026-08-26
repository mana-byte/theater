"""Bounded reverse paging for JSONL transcript histories."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES,
    TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES,
    TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES,
)
from theater.harness.contracts.events import Event
from theater.harness.contracts.source import bound_history_event
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact


class HistoryPageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PageReadResult:
    events: list[Event]
    facts: list[TrajectoryFact]
    trajectory_events: list[Event]
    start: int
    page_end: int
    start_index: int | None
    page_end_index: int | None
    identity: dict[str, object]
    older_identity: dict[str, object]


ParseRecord = Callable[[str, int], ParsedRecord]
DecorateParsed = Callable[[ParsedRecord, int], ParsedRecord]
PrepareHistoryParse = Callable[[BinaryIO, int], None]


class HistoryReader:
    """Read immutable bounded pages while adapters supply their parsing hooks."""

    def __init__(
        self,
        *,
        parse_record: ParseRecord,
        decorate_parsed: DecorateParsed,
        prepare_history_parse: PrepareHistoryParse,
    ) -> None:
        self._parse_record = parse_record
        self._decorate_parsed = decorate_parsed
        self._prepare_history_parse = prepare_history_parse

    def read_page(
        self,
        path: Path,
        *,
        end: int | None,
        end_index: int | None,
        live_offset: int | None,
        limit: int,
        expected_identity: dict[str, object] | None,
    ) -> PageReadResult:
        with path.open("rb") as fh:
            stat = os.fstat(fh.fileno())
            total = int(stat.st_size)
            if expected_identity is not None:
                self.validate_page_identity(fh, stat, expected_identity)
                snapshot_size = cast(int, expected_identity["size"])
            else:
                snapshot_size = total
            page_end = total if end is None else end
            if page_end < 0 or page_end > snapshot_size or page_end > total:
                raise ValueError("history cursor is outside the transcript")
            base_identity = (
                dict(expected_identity)
                if expected_identity is not None
                else self.page_file_identity(fh, stat, boundary_offset=page_end)
            )
            page_end_index = end_index
            if end is None and live_offset != total:
                page_end_index = None
            lines = self.read_page_lines(fh, page_end)
            selected_position = max(0, len(lines) - limit)
            history_start = lines[selected_position][0] if lines else page_end
            self._prepare_history_parse(fh, history_start)
            if page_end_index is None:
                line_index_base = 0
                selected_end_index: int | None = None
            else:
                line_index_base = page_end_index - len(lines)
                selected_end_index = page_end_index
            events, facts, trajectory_events, start, start_index = self.select_page_records(
                lines,
                line_index_base=line_index_base,
                page_end_index=page_end_index,
                page_end=page_end,
                limit=limit,
            )
            return PageReadResult(
                events=events,
                facts=facts,
                trajectory_events=trajectory_events,
                start=start,
                page_end=page_end,
                start_index=start_index,
                page_end_index=selected_end_index,
                identity=self.identity_at_boundary(fh, base_identity, page_end),
                older_identity=self.identity_at_boundary(fh, base_identity, start),
            )

    @classmethod
    def read_page_lines(cls, fh: BinaryIO, page_end: int) -> list[tuple[int, bytes]]:
        scan_start, data = cls.read_reverse_window(fh, page_end)
        at_boundary = scan_start == 0 or cls.byte_at(fh, scan_start - 1) == b"\n"
        if scan_start and not at_boundary:
            first_newline = data.find(b"\n")
            if first_newline < 0:
                raise HistoryPageError(
                    "history_record_too_large",
                    "history page cannot bound a record within the reverse scan limit",
                )
            data = data[first_newline + 1 :]
            scan_start += first_newline + 1
        lines: list[tuple[int, bytes]] = []
        offset = scan_start
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                break
            lines.append((offset, raw[:-1]))
            offset += len(raw)
        return lines

    def select_page_records(
        self,
        lines: list[tuple[int, bytes]],
        *,
        line_index_base: int,
        page_end_index: int | None,
        page_end: int,
        limit: int,
    ) -> tuple[list[Event], list[TrajectoryFact], list[Event], int, int | None]:
        selected_position = max(0, len(lines) - limit)
        parsed_lines: list[
            tuple[int, int, tuple[Event, ...], tuple[TrajectoryFact, ...], tuple[Event, ...]]
        ] = []
        for position in range(selected_position, len(lines)):
            record_offset, raw = lines[position]
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                parsed_lines.append((position, record_offset, (), (), ()))
                continue
            parsed = self._parse_record(line, line_index_base + position)
            decorated = self._decorate_parsed(parsed, record_offset)
            parsed_lines.append(
                (
                    position,
                    record_offset,
                    tuple(
                        bound_history_event(event)
                        for event in decorated.events
                        if not event.usage_only
                    ),
                    tuple(decorated.trajectory),
                    tuple(bound_history_event(event) for event in decorated.baseline_events),
                )
            )

        events: list[Event] = []
        facts: list[TrajectoryFact] = []
        trajectory_events: list[Event] = []
        selected: list[tuple[int, int]] = []
        for (
            position,
            record_offset,
            candidate_events,
            candidate_facts,
            candidate_trajectory_events,
        ) in reversed(parsed_lines):
            if len(candidate_events) > limit or len(candidate_facts) > limit:
                if not selected:
                    raise HistoryPageError(
                        "history_record_too_large",
                        "one transcript record exceeds the history page limit",
                    )
                break
            if (
                len(events) + len(candidate_events) > limit
                or len(facts) + len(candidate_facts) > limit
                or len(trajectory_events) + len(candidate_trajectory_events) > limit
            ):
                break
            selected.append((position, record_offset))
            events[0:0] = candidate_events
            facts[0:0] = candidate_facts
            trajectory_events[0:0] = candidate_trajectory_events
        selected.reverse()
        if selected:
            start_position, start = selected[0]
            start_index = line_index_base + start_position if page_end_index is not None else None
        else:
            start = page_end
            start_index = None
        return events, facts, trajectory_events, start, start_index

    @staticmethod
    def byte_at(fh: BinaryIO, offset: int) -> bytes:
        fh.seek(offset)
        return fh.read(1)

    @classmethod
    def read_reverse_window(cls, fh: BinaryIO, page_end: int) -> tuple[int, bytes]:
        if page_end <= 0:
            return 0, b""
        minimum = max(0, page_end - TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES)
        scan_start = max(0, page_end - TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES)
        fh.seek(scan_start)
        data = fh.read(page_end - scan_start)
        while scan_start > minimum:
            at_boundary = scan_start == 0 or cls.byte_at(fh, scan_start - 1) == b"\n"
            first_newline = data.find(b"\n")
            if at_boundary or (first_newline >= 0 and first_newline < len(data) - 1):
                break
            new_start = max(minimum, scan_start - TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES)
            fh.seek(new_start)
            prefix = fh.read(scan_start - new_start)
            data = prefix + data
            scan_start = new_start
        at_boundary = scan_start == 0 or cls.byte_at(fh, scan_start - 1) == b"\n"
        first_newline = data.find(b"\n")
        if not at_boundary and (first_newline < 0 or first_newline == len(data) - 1):
            raise HistoryPageError(
                "history_record_too_large",
                "history page cannot bound a record within the reverse scan limit",
            )
        return scan_start, data

    @staticmethod
    def page_path_key(path: Path) -> str:
        return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]

    @classmethod
    def encode_page_cursor(
        cls, path: Path, end: int, end_index: int | None, identity: dict[str, object]
    ) -> str:
        payload = {
            "v": 3,
            "path": cls.page_path_key(path),
            "identity": identity,
            "end": end,
            "end_index": end_index,
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return "trj1." + base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    @classmethod
    def decode_page_cursor(
        cls, cursor: str | None, path: Path
    ) -> tuple[int, int | None, dict[str, object]]:
        if not isinstance(cursor, str) or not cursor.startswith("trj1."):
            raise ValueError("history cursor is not valid for a transcript source")
        try:
            if len(cursor.encode("utf-8")) > TRAJECTORY_CURSOR_MAX_BYTES:
                raise ValueError("history cursor is too large")
        except UnicodeEncodeError as exc:
            raise ValueError("history cursor is malformed") from exc
        try:
            raw = base64.urlsafe_b64decode(cursor[5:] + "=" * (-len(cursor[5:]) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
            raise ValueError("history cursor is malformed") from exc
        if not isinstance(payload, dict):
            raise ValueError(  # noqa: TRY004
                "history cursor does not belong to this transcript source"
            )
        end = payload.get("end")
        end_index = payload.get("end_index")
        if (
            set(payload) != {"v", "path", "identity", "end", "end_index"}
            or payload.get("v") != 3
            or payload.get("path") != cls.page_path_key(path)
            or type(end) is not int
            or end < 0
            or (end_index is not None and type(end_index) is not int)
            or (isinstance(end_index, int) and end_index < 0)
            or not isinstance(payload.get("identity"), dict)
        ):
            raise ValueError("history cursor does not belong to this transcript source")
        identity = cast(dict[str, object], payload["identity"])
        return end, cast(int | None, end_index), identity

    @classmethod
    def page_file_identity(
        cls,
        fh: BinaryIO,
        stat: os.stat_result,
        *,
        snapshot_size: int | None = None,
        boundary_offset: int | None = None,
    ) -> dict[str, object]:
        sample_size = TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES
        size = int(stat.st_size) if snapshot_size is None else snapshot_size
        boundary = size if boundary_offset is None else boundary_offset
        if boundary < 0 or boundary > size:
            raise ValueError("history cursor boundary is outside the transcript")
        fh.seek(0)
        head = fh.read(min(sample_size, size))
        boundary_start = max(0, boundary - sample_size)
        fh.seek(boundary_start)
        boundary_bytes = fh.read(boundary - boundary_start)
        return {
            "dev": int(stat.st_dev),
            "ino": int(stat.st_ino),
            "size": size,
            "mtime_ns": int(stat.st_mtime_ns),
            "ctime_ns": int(stat.st_ctime_ns),
            "head": base64.urlsafe_b64encode(head).decode("ascii"),
            "boundary_offset": boundary,
            "boundary": base64.urlsafe_b64encode(boundary_bytes).decode("ascii"),
        }

    @classmethod
    def identity_at_boundary(
        cls, fh: BinaryIO, base: dict[str, object], boundary_offset: int
    ) -> dict[str, object]:
        snapshot_size = base["size"]
        if type(snapshot_size) is not int or boundary_offset < 0 or boundary_offset > snapshot_size:
            raise ValueError("history cursor boundary is outside the transcript")
        start = max(0, boundary_offset - TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES)
        fh.seek(start)
        boundary = fh.read(boundary_offset - start)
        identity = dict(base)
        identity["boundary_offset"] = boundary_offset
        identity["boundary"] = base64.urlsafe_b64encode(boundary).decode("ascii")
        return identity

    @classmethod
    def validate_page_identity(
        cls, fh: BinaryIO, stat: os.stat_result, expected: dict[str, object]
    ) -> None:
        required = {
            "dev",
            "ino",
            "size",
            "mtime_ns",
            "ctime_ns",
            "head",
            "boundary_offset",
            "boundary",
        }
        if set(expected) != required:
            raise ValueError("history cursor is invalid because its identity is malformed")
        dev = expected["dev"]
        ino = expected["ino"]
        snapshot_size = expected["size"]
        mtime_ns = expected["mtime_ns"]
        ctime_ns = expected["ctime_ns"]
        head_value = expected["head"]
        boundary_offset = expected["boundary_offset"]
        boundary_value = expected["boundary"]
        if not (
            type(dev) is int
            and type(ino) is int
            and type(snapshot_size) is int
            and snapshot_size >= 0
            and type(mtime_ns) is int
            and type(ctime_ns) is int
            and isinstance(head_value, str)
            and type(boundary_offset) is int
            and 0 <= boundary_offset <= snapshot_size
            and isinstance(boundary_value, str)
        ):
            raise ValueError("history cursor is invalid because its identity is malformed")
        try:
            head = base64.urlsafe_b64decode(head_value)
            boundary = base64.urlsafe_b64decode(boundary_value)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValueError("history cursor is invalid because its identity is malformed") from exc
        if int(stat.st_dev) != dev or int(stat.st_ino) != ino:
            raise ValueError("history cursor is invalid because the transcript changed")
        if int(stat.st_size) < snapshot_size:
            raise ValueError("history cursor is invalid because the transcript shrank")
        if boundary_offset > int(stat.st_size):
            raise ValueError("history cursor is invalid because its boundary is unavailable")
        if int(stat.st_size) == snapshot_size and (
            int(stat.st_mtime_ns) != mtime_ns or int(stat.st_ctime_ns) != ctime_ns
        ):
            raise ValueError("history cursor is invalid because the transcript changed")
        current = cls.page_file_identity(
            fh,
            stat,
            snapshot_size=snapshot_size,
            boundary_offset=boundary_offset,
        )
        if current["head"] != expected["head"]:
            raise ValueError("history cursor is invalid because the transcript prefix changed")
        if current["boundary"] != expected["boundary"]:
            raise ValueError("history cursor is invalid because the transcript prefix changed")
        if (
            len(head) > TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES
            or len(boundary) > TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES
        ):
            raise ValueError("history cursor is invalid because its identity is malformed")
