"""Where an observer gets its input, and what it hands back.

The observer does two jobs. It *gets* what an agent said, and it *decides* what
that means — idle or working, turn over, job finished, participant dead. The
second job is identical for every harness and is where every observation bug in
this project has been. The first job is different for every harness: vibe and
claude append JSONL, opencode writes a shared SQLite database, and a future one
may only offer an HTTP event stream.

So the first job is the seam. A `Source` produces `Batch`es; the observer owns
everything that happens to them. An adapter that writes a transcript gets
`TranscriptSource` for free by subclassing `TranscriptObserver`, which is why
three of the four shipped adapters do not mention any of this.

What a source may and may not do
--------------------------------
A source reports facts. It does not touch the registry, the bus or the job
manager — not because it could not, but because the moment two sources can, the
policy that used to live in one place lives in as many places as there are
harnesses, and the fix we shipped for one is missing from the rest.

Immutability, and why `Batch.status` exists
-------------------------------------------
Tailing an append-only file gives a strong guarantee: a byte offset is a proof
that everything before it is final. A source reading a mutable store has only a
watermark — rows behind the cursor may still change. Such a source must hold a
record back until it is terminal rather than emit something it cannot retract,
and it should report `status` directly instead of letting the observer infer
one from silence. The quiet timers exist for sources that cannot tell us; a
source that knows is believed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from theater.harness.base import Event
from theater.models import Status

if TYPE_CHECKING:
    from theater.harness.observation import TranscriptObserver

logger = logging.getLogger("theater.harness.source")


def attach_point(path: Path) -> tuple[int, int, int, str | None]:
    """Byte offset, record count, mtime, and last complete line at end of file.

    The mtime is taken *after* the read, from the same descriptor, so it always
    covers every byte counted here even if a writer appended mid-scan.

    The last complete line is returned so the caller can derive an initial
    status from it without replaying history onto the bus. A spawned agent
    that finishes its turn before the observer attaches would otherwise stay
    STARTING forever: no new bytes arrive after attach, so nothing else fires.
    """
    size = 0
    lines = 0
    tail: list[bytes] = []
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            size += len(chunk)
            lines += chunk.count(b"\n")
            tail.append(chunk)
        mtime = os.fstat(fh.fileno()).st_mtime_ns
    last_line: str | None = None
    if lines > 0:
        data = b"".join(tail)
        head, sep, _rest = data.rpartition(b"\n")
        if sep:
            # head is everything before the last newline; the last complete
            # line is the portion after the second-to-last newline (or the
            # whole head if there is only one line).
            _prefix, _sep2, last_bytes = head.rpartition(b"\n")
            last_line = last_bytes.decode("utf-8", errors="replace")
    return size, lines, mtime, last_line


@dataclass(frozen=True, slots=True)
class Attachment:
    """Where a source started reading, reported once each time it (re)attaches.

    `location` is whatever names the input to a human reading the bus: a file
    path today, a session id or a URL for a source that has no file. It is
    published as the `path` field of the `agent.transcript` event, which
    predates this module and is what the régie renders.

    `last_event` is the final event of the last record skipped at attach, and
    it is the reason a spawned agent that finished before we found it does not
    sit at STARTING forever. It is deliberately not put on the bus: attaching
    skips history rather than replaying it. Note that only the *last* event of
    that record is carried — every shipped parser puts a turn boundary on the
    final event of a record, so nothing is lost, but a parser that did
    otherwise would have its boundary missed at attach time only.
    """

    location: str
    session_id: str | None = None
    skipped: int = 0
    last_event: Event | None = None


@dataclass(frozen=True, slots=True)
class History:
    """A session's past, read back in full. What `read_transcript` asks for.

    Separate from `read()` because it answers a different question. Polling
    asks "what is new"; this asks "what was said", and the answer must be
    unclipped — the entire reason the tool exists is that the bus clips to
    `MAX_TEXT` and an agent sometimes needs the whole reply.

    It is a method on the source rather than a free function over a transcript
    path because "where the text lives" is precisely what the source owns. A
    harness with no file cannot answer the path question at all.
    """

    location: str | None = None
    events: Sequence[Event] = ()


@dataclass(frozen=True, slots=True)
class Batch:
    """One poll's worth of facts from a source.

    `progressed` is not the same as "produced events", and conflating them is a
    live bug rather than a tidiness point. Both shipped harnesses write
    bookkeeping records that parse to zero events but do move the file forward.
    That is activity: if it read as silence, the 60s rescue timer would fire in
    the middle of real work and hand a caller a half-finished answer. So a
    source that consumed input says so, even when it has nothing to report.

    The converse is not required. Events imply progress, and the observer
    treats them as such, so a source that emits events without setting
    `progressed` is not punished for it.

    `waiting` means there is nothing to read *from* yet — no transcript on disk,
    no session row in the database. The observer backs off on its search
    interval rather than its poll interval and runs no quiet timers, because
    silence from a source that has not attached is not evidence about the agent.
    """

    events: Sequence[Event] = ()
    progressed: bool = False
    status: Status | None = None
    attached: Attachment | None = None
    waiting: bool = False


class Source(ABC):
    """A live view of one participant's output.

    Constructed per participant by `HarnessObserver.open_source` and polled by
    the reducer until the participant dies. Anything expensive to hold open — a
    file handle, a database connection, an HTTP subscription — belongs here,
    which is the whole reason this is an object and not another method on
    `Harness`.
    """

    @abstractmethod
    async def read(self) -> Batch:
        """Whatever has happened since the last call. Never raises for an
        input that is merely absent — that is `Batch(waiting=True)`."""

    async def refresh(self) -> Batch:
        """Re-check where the input lives, after a stretch of silence.

        Called by the observer on the relocate timer rather than every poll,
        because for a file-backed source this is a directory scan. The default
        is to do nothing: a source whose location cannot change needs no such
        check.
        """
        return Batch()

    async def history(self, *, last_n: int) -> History:
        """The session so far, with text unclipped. Newest `last_n` events.

        Independent of the poll cursor: reading history must not disturb where
        the watcher has got to, because the caller is usually a *different*
        consumer — `read_transcript` opens its own short-lived source rather
        than borrowing the observer's.

        `last_n <= 0` means everything. The default returns nothing, which is
        the honest answer for a source that can only see forward.
        """
        return History()

    async def aclose(self) -> None:
        """Release anything held open. Called once, when the watcher stops."""
        return None


class TranscriptSource(Source):
    """Tail an append-only transcript file. What `TranscriptObserver` returns.

    Holds the byte offset, record index and mtime that used to live on the
    observer's cursor. Nothing above it knows the input is a file.
    """

    def __init__(
        self,
        observer: TranscriptObserver,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> None:
        self._observer = observer
        self._cwd = cwd
        #: Updated when an attach reveals the harness's own session id, so a
        #: later re-attach can use the sharper key the first one lacked.
        self._session_id = session_id
        self._after = after
        self.path: Path | None = None
        self.offset = 0
        self.index = 0
        self.mtime = 0

    async def read(self) -> Batch:
        if self.path is None:
            attached = await self._attach()
            return Batch(attached=attached) if attached else Batch(waiting=True)
        try:
            return self._drain()
        except FileNotFoundError:
            # The transcript was deleted or rotated out from under us. Drop
            # back to searching rather than letting the watcher die.
            self._detach()
            return Batch(waiting=True)

    async def refresh(self) -> Batch:
        """Move to the newest transcript if the harness started a new one.

        Located by cwd alone, ignoring the session id: vibe opens a new session
        directory every turn, and the id we stored pins `find_transcript` to
        the first one, which never grows again.

        The same path back means the agent is idle rather than rotated, and
        returns an empty batch so the observer's timers keep counting — the
        relocate check must not reset the clock the screen check reads.
        """
        path = await self._locate(session_id=None)
        if path is None or path == self.path:
            return Batch()
        logger.info("transcript rotated: %s -> %s", self.path, path)
        attached = await self._attach(path)
        return Batch(attached=attached) if attached else Batch()

    async def history(self, *, last_n: int) -> History:
        """Re-read the whole transcript with the text left unclipped.

        Located from scratch rather than reusing `self.path`, so this works on
        a source that has never polled — which is the normal case, since the
        caller opens one just for this.
        """
        path = self.path or await self._locate(session_id=self._session_id)
        if path is None:
            return History()
        events = await asyncio.to_thread(self._read_all, path)
        return History(
            location=str(path),
            events=events[-last_n:] if last_n > 0 else events,
        )

    def _read_all(self, path: Path) -> list[Event]:
        events: list[Event] = []
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for index, line in enumerate(fh):
                    line = line.strip()
                    if line:
                        events.extend(self._observer.parse(line, index, clip_text=False))
        except OSError:
            # A transcript that vanished mid-read is the same non-event here as
            # it is in the poll path: report what there is, which is nothing.
            return []
        return events

    # ---- internals ------------------------------------------------------

    def _detach(self) -> None:
        self.path = None
        self.offset = self.index = self.mtime = 0

    async def _locate(self, *, session_id: str | None) -> Path | None:
        if not self._cwd:
            return None
        return await asyncio.to_thread(
            self._observer.find_transcript,
            cwd=self._cwd,
            session_id=session_id,
            after=self._after,
        )

    async def _attach(self, path: Path | None = None) -> Attachment | None:
        """Point at the end of a transcript. None if there is not one yet.

        Pass `path` to adopt a known file (a rotation); omit it to go looking.
        """
        if path is None:
            path = await self._locate(session_id=self._session_id)
            if path is None:
                return None
        size, lines, mtime, last_line = await asyncio.to_thread(attach_point, path)
        self.path, self.offset, self.index, self.mtime = path, size, lines, mtime
        session_id = self._observer.session_id(path)
        if session_id:
            self._session_id = session_id
        last_event: Event | None = None
        if last_line is not None:
            parsed = self._observer.parse(last_line, lines - 1)
            last_event = parsed[-1] if parsed else None
        return Attachment(
            location=str(path),
            session_id=session_id,
            skipped=lines,
            last_event=last_event,
        )

    def _drain(self) -> Batch:
        """Read whatever the transcript grew by.

        Runs on the event loop rather than in a thread. It is a read of the
        bytes appended since the last poll — usually none, occasionally a few
        kilobytes — and the parse is pure, so the thread hop would cost more
        than it saves.
        """
        assert self.path is not None
        path, offset, index, mtime = self.path, self.offset, self.index, self.mtime

        st = path.stat()
        size = st.st_size
        # Size alone cannot tell "nothing happened" from "rewritten to the same
        # length", and guessing wrong is not a missed event but a corrupt one:
        # the offset would land mid-record and every later parse would be
        # garbage. So a file that changed without growing is treated as rotated.
        if size < offset or (size == offset and st.st_mtime_ns != mtime):
            logger.info("transcript %s was rewritten; re-reading from the top", path)
            offset = index = 0
        if size == offset:
            self.mtime = st.st_mtime_ns
            return Batch()

        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
            mtime = os.fstat(fh.fileno()).st_mtime_ns
        head, sep, _tail = data.rpartition(b"\n")
        if not sep:
            # A record is still being written. Leave the offset alone and read
            # the whole thing again next tick; partial JSON is not parseable
            # and buffering it here would duplicate what the file already does.
            self.mtime = mtime
            return Batch()
        offset += len(head) + 1

        events: list[Event] = []
        for raw in head.split(b"\n"):
            line = raw.decode("utf-8", errors="replace")
            events.extend(self._observer.parse(line, index))
            index += 1

        progressed = offset != self.offset
        self.offset, self.index, self.mtime = offset, index, mtime
        return Batch(events=events, progressed=progressed)
