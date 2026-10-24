"""Claude transcript source and history setup."""

from __future__ import annotations

import asyncio
import errno
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from theater.harness.channels.filesystem import WatchGate
from theater.harness.contracts.events import clip
from theater.harness.normalization.timing import iso_epoch
from theater.harness.normalization.values import decode_json_record
from theater.harness.source import (
    Attachment,
    AttachSnapshot,
    Batch,
    ReceiptAdmission,
    TranscriptSource,
)
from theater.harness.transcript.history import HistoryParseContext
from theater.provenance import TranscriptProvenance

from .constants import SEAL_NODE_LIMIT

if TYPE_CHECKING:
    from .observer import ClaudeCodeObserver


def _open_claude_source(
    *,
    root: Path,
    relocate_by_cwd: bool,
    cwd: str | None,
    session_id: str | None = None,
    after: float | None = None,
    session_provenance: str | TranscriptProvenance | None = None,
    known_location: str | None = None,
) -> _ClaudeSource:
    from .observer import ClaudeCodeObserver

    return _ClaudeSource(
        ClaudeCodeObserver(root=root),
        cwd=cwd,
        session_id=session_id,
        after=after,
        allow_refresh=relocate_by_cwd,
        session_provenance=session_provenance,
        known_location=known_location,
    )


#: Short, because a relocated receipt file is only found by the real check.
_RECEIPT_FALLBACK_SECONDS = 2.0


def _live_turn_start(
    records: Sequence[tuple[int, int, str]], after: float | None, session_id: str | None
) -> tuple[int, int] | None:
    """Locate a launch-local user turn assuming nondecreasing same-machine wall time."""
    # No predecessor floor is supplied; copied history cannot be fenced by record index.
    if after is None or session_id is None:
        return None
    previous = 0 if records and records[0][0] == 0 else None
    for index, offset, line in records:
        record = decode_json_record(line)
        if (
            previous is not None
            and record is not None
            and record.get("type") == "user"
            and record.get("isSidechain") is not True
            and record.get("sessionId") == session_id
            and (timestamp := iso_epoch(record.get("timestamp"))) is not None
            and timestamp > after
        ):
            return index, previous
        previous = offset
    return None


class _ClaudeSource(TranscriptSource):
    """Keep a receipt pending until its JSONL materializes.

    A receipt path is not identity loss until the file has existed.
    """

    if TYPE_CHECKING:
        _observer: ClaudeCodeObserver

    #: The seal's own node bound: all the causal context a live read could need.
    _attach_tail = SEAL_NODE_LIMIT

    def __init__(self, observer: ClaudeCodeObserver, **kwargs) -> None:
        super().__init__(observer, **kwargs)
        # A restored pin is history; only an unbound launch may have an unread current turn.
        self._live_after = self._after if self._known_location is None else None
        self._expected_location: Path | None = None
        #: A receipt path is stat-polled only when its directory changed or the safety net is due.
        self._receipt_gate = WatchGate(fallback_seconds=_RECEIPT_FALLBACK_SECONDS)

    async def _stage_attachment(
        self, snapshot: AttachSnapshot, attachment: Attachment
    ) -> Attachment:
        """Prime the causal seal from the scanned tail; a launch-local turn stays for live reads."""
        if snapshot.trailing_bytes:
            raise OSError(errno.ESTALE, "transcript attachment ends in an incomplete record")
        point = attachment.point
        assert point is not None
        # The base attach parsed the last record alone; restart so the seal sees the tail in order.
        self._observer.reset_stream()
        live_start = _live_turn_start(snapshot.tail, self._live_after, attachment.session_id)
        ends = {record.raw_index: record.end for record in snapshot.tail}
        last_event = None
        for index, _, line in snapshot.tail:
            if live_start is not None and index == live_start[0]:
                break
            for event in self._observer.parse(line, index, clip_text=False):
                if not event.usage_only:
                    last_event = replace(event, text=clip(event.text))
                    point = replace(point, records=event.raw_index + 1, size=ends[event.raw_index])
            if index % 32 == 0:
                await asyncio.sleep(0)
        if live_start is not None:
            # Copied history primes the seal; the current turn settles only through live reads.
            self._restage(index=live_start[0], offset=live_start[1])
            attachment = replace(attachment, status=None)
            last_event = None
        # Metadata after a boundary must not advance that boundary past a resume floor.
        return replace(attachment, last_event=last_event, point=point)

    def commit_attachment(self) -> None:
        super().commit_attachment()
        self._live_after = None

    def bind_wakeup(self, wakeup) -> None:
        super().bind_wakeup(wakeup)
        self._receipt_gate.set_wake(wakeup)

    async def aclose(self) -> None:
        self._receipt_gate.release()
        await super().aclose()

    def _history_parse_context(self) -> HistoryParseContext:
        """History parses on a fresh clone so it never disturbs the live observer."""
        from .observer import ClaudeCodeObserver

        reader = ClaudeCodeObserver(root=self._observer.root)
        return HistoryParseContext(
            parse_record=lambda line, index: reader.parse_record(line, index, clip_text=False),
            prepare_history_parse=reader._seed_mcp_context,
        )

    async def read(self) -> Batch:
        self._require_decision()
        path = self._expected_location
        if path is None:
            self._receipt_gate.release()
            return await super().read()
        if not self._receipt_gate.due(path):
            return Batch(waiting=True)
        try:
            path.stat()
        except FileNotFoundError:
            relocated = await self._locate(session_id=self._session_id)
            if relocated is None or self._observer.session_id(relocated) != self._session_id:
                return Batch(waiting=True)
            path = relocated
        except OSError as exc:
            return self._source_unavailable_batch(exc)

        self._expected_location = None
        self._known_location = path
        self._known_location_provenance = TranscriptProvenance.EXACT
        self._proven[path] = TranscriptProvenance.EXACT
        return await super().read()

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        path = Path(location)
        self._pending = None
        self._session_id = session_id
        self._session_provenance = TranscriptProvenance.EXACT
        self._proven[path] = TranscriptProvenance.EXACT
        if self.path == path:
            self._expected_location = None
            self._known_location = path
            self._known_location_provenance = TranscriptProvenance.EXACT
            return "accepted"

        self._expected_location = path
        self._known_location = None
        self._known_location_provenance = TranscriptProvenance.HEURISTIC
        self._detach()
        return "staged"

    def revoke_attachment(self) -> None:
        self._expected_location = None
        super().revoke_attachment()
