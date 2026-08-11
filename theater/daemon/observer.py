"""Watch what agents are doing by tailing the transcripts they already write.

Why tailing and not instrumentation
-----------------------------------
The alternative was to require each agent to report its own activity over MCP.
That fails for the case Theater exists to serve: an agent that is mid-tool-call
is not making MCP calls, which is exactly when you most want to know what it is
doing. Worse, it would only work for agents cooperative enough to call us, so
an adopted session started before Theater existed would stay invisible.

Both harnesses already write a complete, append-only record of every turn to
disk. Reading it needs no cooperation from the agent, works identically for
spawned and adopted participants, and cannot be forgotten by a harness author.

Attach at EOF, always
---------------------
A participant we adopt may have a 3 MB transcript behind it. Replaying that
onto the bus would flood it with history that is neither live nor interesting.
So attaching skips to the current end of file and counts the records it
skipped, so record indices stay true. For a freshly spawned agent the file is
empty and the rule costs nothing — one behaviour, no special case.

What this cannot see
--------------------
Only IDLE and WORKING are derivable here; see harness/base.py for why
AWAITING_INPUT needs the rendered screen instead. A participant blocked on a
permission prompt will read as WORKING until phase 5b.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from theater.daemon.registry import Registry
from theater.harness import HARNESSES, Harness, status_after
from theater.models import Status, Tier

logger = logging.getLogger("theater.observer")

#: How often to check a known transcript for new bytes. Faster than the reaper
#: because this drives what the régie renders, and a second of lag on "what is
#: it doing right now" is visible to a human watching.
POLL_INTERVAL = 0.25

#: How long to wait with no new bytes before re-locating the transcript.
#: Vibe starts a new session directory on each turn; if the observer
#: is locked onto the old file, it needs to re-scan to find the new one.
RELOCATE_TIMEOUT = 5.0

#: How often to look for a transcript we have not found yet. Slower, because
#: it is a directory scan rather than a stat.
SEARCH_INTERVAL = 2.0

#: How often to reconcile the watch tasks against the registry.
SYNC_INTERVAL = 1.0


def _attach_point(path: Path) -> tuple[int, int, int, str | None]:
    """Byte offset, record count, mtime, and last complete line at end of file.

    The mtime is taken *after* the read, from the same descriptor, so it always
    covers every byte counted here even if a writer appended mid-scan.

    The last complete line is returned so the caller can derive an initial
    status from it without replaying history onto the bus. A spawned agent
    that finishes its turn before the observer attaches would otherwise stay
    STARTING forever: no new bytes arrive after attach, so _drain never fires.
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


class Observer:
    def __init__(
        self,
        registry: Registry,
        harnesses: dict[str, Harness] | None = None,
        *,
        poll: float = POLL_INTERVAL,
        search: float = SEARCH_INTERVAL,
        sync: float = SYNC_INTERVAL,
        jobs=None,
    ):
        self.registry = registry
        self.store = registry.store
        #: Injectable, and an empty map is a legitimate value meaning "observe
        #: nothing" — which is what socket-level tests want, since the real
        #: harness roots point at the user's own ~/.claude and ~/.vibe.
        self.harnesses = HARNESSES if harnesses is None else harnesses
        self.poll = poll
        self.search = search
        self.sync = sync
        #: Optional JobManager. When set, turn-end events for a participant
        #: with a running job finish that job with the assistant text as
        #: the result.
        self.jobs = jobs
        self._tasks: dict[str, asyncio.Task] = {}
        #: Participants whose watcher ended by itself. Not restarted: whatever
        #: stopped it will stop it again, and a respawn loop would be worse
        #: than a blind spot.
        self._retired: set[str] = set()
        #: Participants we cannot observe, warned about once each. `hello`
        #: accepts any harness string, so a session that misreports its own
        #: harness would otherwise be invisible with nothing anywhere saying so.
        self._unobservable: set[str] = set()
        self._supervisor: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if not self.harnesses:
            logger.debug("no harnesses configured; observation disabled")
            return
        self._supervisor = asyncio.create_task(self._supervise())

    async def aclose(self) -> None:
        self._stopping.set()
        tasks = list(self._tasks.values())
        if self._supervisor:
            tasks.append(self._supervisor)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._supervisor = None

    async def _sleep(self, seconds: float) -> None:
        """Wait, but wake immediately on shutdown."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    # ---- supervision ---------------------------------------------------

    async def _supervise(self) -> None:
        while not self._stopping.is_set():
            try:
                self._reconcile()
            except Exception:
                logger.exception("observer reconcile failed")
            await self._sleep(self.sync)

    def _reconcile(self) -> None:
        live = {p.id: p for p in self.registry.list()}
        for pid, task in list(self._tasks.items()):
            if pid in live and not task.done():
                continue
            self._tasks.pop(pid)
            task.cancel()
            if pid in live:
                self._retired.add(pid)
                logger.warning("observer for %s stopped; not restarting", pid)
        for pid, p in live.items():
            if pid in self._tasks or pid in self._retired:
                continue
            if p.harness not in self.harnesses or not p.cwd:
                self._warn_unobservable(pid, p)
                continue
            self._unobservable.discard(pid)
            self._tasks[pid] = asyncio.create_task(self._watch(pid, p.harness))

    def _warn_unobservable(self, pid: str, p) -> None:
        if pid in self._unobservable:
            return
        self._unobservable.add(pid)
        if not p.cwd:
            reason = "it reported no working directory"
        else:
            known = ", ".join(sorted(self.harnesses)) or "none"
            reason = f"harness {p.harness!r} is not one we can read (known: {known})"
        logger.warning("cannot observe %s: %s", pid, reason)

    # ---- one participant -----------------------------------------------

    async def _watch(self, pid: str, harness_name: str) -> None:
        harness = self.harnesses[harness_name]
        path: Path | None = None
        offset = 0
        index = 0
        mtime = 0
        last_growth: float | None = None
        while not self._stopping.is_set():
            try:
                if path is None:
                    path = await self._locate(pid, harness)
                    if path is None:
                        await self._sleep(self.search)
                        continue
                    offset, index, mtime, last_line = await asyncio.to_thread(_attach_point, path)
                    self._on_attach(pid, harness, path, index, last_line)
                    last_growth = None
                new_offset, new_index, new_mtime = self._drain(
                    pid, harness, path, offset, index, mtime
                )
                if new_offset != offset:
                    # The file grew; reset the stale timer.
                    last_growth = None
                else:
                    # No growth this tick.
                    if last_growth is None:
                        import time

                        last_growth = time.monotonic()
                    elif time.monotonic() - last_growth > RELOCATE_TIMEOUT:
                        # The transcript hasn't grown in a while. Vibe
                        # may have rotated to a new session directory
                        # (it starts a new one on each turn). Drop back
                        # to searching to find the new transcript.
                        logger.info(
                            "transcript for %s stale; re-locating", pid
                        )
                        path = None
                        offset = index = mtime = 0
                        last_growth = None
                        continue
                offset, index, mtime = new_offset, new_index, new_mtime
            except asyncio.CancelledError:
                raise
            except FileNotFoundError:
                # Transcript went away: the session was deleted or rotated.
                # Drop back to searching rather than dying.
                path = None
                offset = index = mtime = 0
            except Exception:
                logger.exception("observing %s failed", pid)
            await self._sleep(self.poll)

    async def _locate(self, pid: str, harness: Harness) -> Path | None:
        p = self.store.get_participant(pid)
        if p is None or not p.cwd:
            return None
        # A spawned participant's transcript cannot predate the participant, so
        # its creation time is a safe floor. An adopted one's transcript almost
        # certainly does predate our first sight of it, so there is no floor to
        # apply — see Harness.find_transcript.
        after = p.created_at if p.tier is Tier.SPAWNED else None
        return await asyncio.to_thread(
            harness.find_transcript,
            cwd=p.cwd,
            session_id=p.session_id,
            after=after,
        )

    def _on_attach(
        self, pid: str, harness: Harness, path: Path, skipped: int, last_line: str | None
    ) -> None:
        p = self.store.get_participant(pid)
        session_id = harness.session_id(path)
        if p is not None and session_id and p.session_id != session_id:
            p.session_id = session_id
            self.store.upsert_participant(p)
        self.store.bus_append(
            "agent.transcript",
            to_id=pid,
            payload={"path": str(path), "skipped_records": skipped},
        )
        logger.info("observing %s at %s (+%d existing records)", pid, path, skipped)
        # Derive an initial status from the last record we skipped, so a
        # spawned agent that finished its turn before we attached does not
        # stay STARTING forever. The bus gets no history replayed — only the
        # status moves. An empty file (no last_line) leaves status as-is.
        if last_line is not None:
            for event in harness.parse(last_line, skipped - 1):
                self._settle(pid, status_after(event))
                if event.turn_end and self.jobs is not None:
                    self._finish_jobs_for_turn(pid, event.text)

    def _drain(
        self,
        pid: str,
        harness: Harness,
        path: Path,
        offset: int,
        index: int,
        mtime: int,
    ) -> tuple[int, int, int]:
        st = path.stat()
        size = st.st_size
        # Size alone cannot tell "nothing happened" from "rewritten to the same
        # length", and guessing wrong is not a missed event but a corrupt one:
        # the offset would land mid-record and every later parse would be
        # garbage. So a file that changed without growing is treated as rotated.
        if size < offset or (size == offset and st.st_mtime_ns != mtime):
            logger.info("transcript for %s was rewritten; re-reading from the top", pid)
            offset = index = 0
        if size == offset:
            return offset, index, st.st_mtime_ns
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
            mtime = os.fstat(fh.fileno()).st_mtime_ns
        head, sep, _tail = data.rpartition(b"\n")
        if not sep:
            # A record is still being written. Leave the offset alone and read
            # the whole thing again next tick; partial JSON is not parseable
            # and buffering it here would duplicate what the file already does.
            return offset, index, mtime
        offset += len(head) + 1

        last = None
        for raw in head.split(b"\n"):
            line = raw.decode("utf-8", errors="replace")
            for event in harness.parse(line, index):
                self.store.bus_append(
                    f"agent.{event.kind}",
                    from_id=pid,
                    payload={
                        "text": event.text,
                        "tool": event.tool_name,
                        # The harness's own clock, null when it keeps none. The
                        # bus row's own ts is observation time, which is a
                        # different quantity; do not conflate them.
                        "ts": event.ts,
                        "turn_end": event.turn_end,
                        "index": event.raw_index,
                    },
                )
                last = event
            index += 1

        if last is not None:
            self._settle(pid, status_after(last))
            # If this event ended a turn and the participant has a running
            # job, finish it with the assistant text as the result.
            if last.turn_end and self.jobs is not None:
                self._finish_jobs_for_turn(pid, last.text)
        return offset, index, mtime

    def _settle(self, pid: str, desired: Status) -> None:
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD:
            return
        if p.status is desired:
            # Same status, new evidence: last_activity still has to move, but
            # writing a status event every quarter second would drown the bus.
            self.registry.touch(pid)
        else:
            self.registry.set_status(pid, desired)

    def _finish_jobs_for_turn(self, pid: str, result_text: str) -> None:
        """Finish any running jobs for this participant with the result text.

        Called when the observer detects a turn_end. The result is the
        assistant's text from the turn-end event — clipped to MAX_TEXT by
        the harness parser already. If the participant has no running jobs
        (e.g. it's a hand-started session nobody spawned), this is a no-op.
        """
        if self.jobs is None:
            return
        from theater.daemon.jobs import JobState
        running = self.store.running_jobs_for_target(pid)
        for job in running:
            self.jobs.finish(
                job.handle,
                state=JobState.DONE,
                result=result_text or "",
            )