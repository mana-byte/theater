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
import time
from dataclasses import dataclass
from pathlib import Path

from theater.config import ObserverSection
from theater.daemon.registry import Registry
from theater.harness import HARNESSES, Harness, clip, status_after
from theater.models import JobState, Status, Tier

logger = logging.getLogger("theater.observer")

#: Fallback timings, used when no config is passed in. Each is documented on
#: `config.ObserverSection`, which owns the literal so the value a user can set
#: and the value the code defaults to cannot drift apart. A live daemon passes
#: the loaded config through `Daemon.__init__`; these matter for direct
#: construction, which is mostly tests.
_DEFAULTS = ObserverSection()

POLL_INTERVAL = _DEFAULTS.poll_interval
RELOCATE_TIMEOUT = _DEFAULTS.relocate_timeout
AWAITING_INPUT_TIMEOUT = _DEFAULTS.awaiting_input_timeout
SEARCH_INTERVAL = _DEFAULTS.search_interval
SYNC_INTERVAL = _DEFAULTS.sync_interval
SCREEN_INTERVAL = _DEFAULTS.screen_interval

#: Consecutive idle-looking screens before a turn is called finished. Two, not
#: one: the screen is a rendering, and a harness that clears the pane between
#: phases shows a bare prompt for one frame in the middle of working. Finishing
#: a job there hands the caller a partial answer, which is worse than being a
#: poll late — see `_watch_screen`.
IDLE_CONFIRMATIONS = 2


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


def screen_result(capture: str) -> str:
    """What a screen-derived turn end can offer a waiting caller as a result.

    The visible pane with its trailing prompt line removed. This is not the
    agent's answer: it is one screenful of rendering, banner and all, cut off
    at the top by the pane height and stripped of everything that scrolled
    past. It is the best available for a harness with no transcript, and the
    thinness of it is the price of declaring a harness instead of writing a
    plugin that can read one.
    """
    lines = [line.rstrip() for line in capture.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.pop()  # the prompt itself, which is not part of any answer
    return "\n".join(lines).strip()


@dataclass
class TranscriptCursor:
    """Where one participant's watcher has read to, and how long it has waited.

    Two quiet timers, not one. They measure the same silence but are reset by
    different events, and collapsing them into a single timer is a bug we have
    already shipped once: the relocate fires at 5s and used to reset the clock,
    so the 10s screen check was never reached and AWAITING_INPUT never appeared.
    """

    path: Path | None = None
    offset: int = 0
    index: int = 0
    mtime: int = 0
    #: When the transcript went quiet, for the relocate timer.
    quiet_since: float | None = None
    #: The same silence, for the screen check. Reset independently.
    screen_quiet_since: float | None = None

    def attach(self, path: Path, offset: int, index: int, mtime: int) -> None:
        self.path = path
        self.offset = offset
        self.index = index
        self.mtime = mtime
        self.stir()

    def detach(self) -> None:
        """Forget the file. The watcher drops back to searching for one."""
        self.path = None
        self.offset = self.index = self.mtime = 0
        self.stir()

    def stir(self) -> None:
        """Something happened: both timers start counting again from zero."""
        self.quiet_since = None
        self.screen_quiet_since = None

    def advance(self, offset: int, index: int, mtime: int) -> bool:
        """Take a drain's result. True if the file grew."""
        grew = offset != self.offset
        self.offset, self.index, self.mtime = offset, index, mtime
        return grew

    def begin_quiet(self, now: float) -> None:
        """Start whichever timers are not already running."""
        if self.quiet_since is None:
            self.quiet_since = now
        if self.screen_quiet_since is None:
            self.screen_quiet_since = now

    def quiet_for(self, now: float) -> float:
        return now - (self.quiet_since if self.quiet_since is not None else now)

    def screen_quiet_for(self, now: float) -> float:
        since = self.screen_quiet_since
        return now - (since if since is not None else now)


class Observer:
    def __init__(
        self,
        registry: Registry,
        harnesses: dict[str, Harness] | None = None,
        *,
        poll: float = POLL_INTERVAL,
        search: float = SEARCH_INTERVAL,
        sync: float = SYNC_INTERVAL,
        relocate: float = RELOCATE_TIMEOUT,
        awaiting: float = AWAITING_INPUT_TIMEOUT,
        screen: float = SCREEN_INTERVAL,
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
        self.relocate = relocate
        self.awaiting = awaiting
        self.screen = screen
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
            harness = self.harnesses.get(p.harness)
            if harness is None:
                self._warn_unobservable(pid, p)
                continue
            # A transcript is found by working directory; a screen is found by
            # pane. So cwd is required for one loop and irrelevant to the other.
            if harness.has_transcript and not p.cwd:
                self._warn_unobservable(pid, p)
                continue
            self._unobservable.discard(pid)
            watch = self._watch if harness.has_transcript else self._watch_screen
            self._tasks[pid] = asyncio.create_task(watch(pid, p.harness))

    def _warn_unobservable(self, pid: str, p) -> None:
        if pid in self._unobservable:
            return
        self._unobservable.add(pid)
        if p.harness not in self.harnesses:
            known = ", ".join(sorted(self.harnesses)) or "none"
            reason = f"harness {p.harness!r} is not one we can read (known: {known})"
        else:
            reason = "it reported no working directory"
        logger.warning("cannot observe %s: %s", pid, reason)

    # ---- one participant -----------------------------------------------

    async def _watch(self, pid: str, harness_name: str) -> None:
        harness = self.harnesses[harness_name]
        cursor = TranscriptCursor()

        while not self._stopping.is_set():
            try:
                if cursor.path is None and not await self._open(pid, harness, cursor):
                    await self._sleep(self.search)
                    continue
                drained = self._drain(
                    pid, harness, cursor.path, cursor.offset, cursor.index, cursor.mtime
                )
                if cursor.advance(*drained):
                    cursor.stir()
                    self._unblock(pid)
                else:
                    await self._on_quiet(pid, harness, cursor)
            except asyncio.CancelledError:
                raise
            except FileNotFoundError:
                # Transcript went away: the session was deleted or rotated.
                # Drop back to searching rather than dying.
                cursor.detach()
            except Exception:
                logger.exception("observing %s failed", pid)
            await self._sleep(self.poll)

    async def _watch_screen(self, pid: str, harness_name: str) -> None:
        """Derive status from the rendered screen, for a parser-less harness.

        A harness declared in `[harness.*]` has no transcript, so `parse`
        returns nothing and no `turn_end` event can ever be produced. Without
        one, `theater_send` would accept a prompt and leave the caller's
        `await_sessions` waiting forever. So here the idle-prompt heuristic is
        promoted from a display hint to a completion signal.

        That inverts the risk profile documented on `is_idle_screen`, which was
        tuned to accept false negatives: a false idle now finishes a job early
        and hands the caller a partial answer. Two mitigations, both narrow.
        The promotion applies only to harnesses that have no parser — never to
        one whose transcript we can read. And an idle screen must hold for
        `IDLE_CONFIRMATIONS` consecutive polls before it counts.

        An unreadable screen decides nothing. Failing to capture is not
        evidence of either state, so the status is left exactly as it was.
        """
        harness = self.harnesses[harness_name]
        idle_streak = 0
        ended = False

        while not self._stopping.is_set():
            try:
                p = self.store.get_participant(pid)
                if p is None or p.status is Status.DEAD:
                    return
                capture = await self._capture(p.tmux_pane) if p.tmux_pane else None
                if capture is not None:
                    idle_streak = idle_streak + 1 if harness.is_idle_screen(capture) else 0
                    if idle_streak >= IDLE_CONFIRMATIONS:
                        if not ended:
                            ended = True
                            self._end_turn_from_screen(pid, capture)
                        self._settle(pid, Status.IDLE)
                    elif idle_streak == 0:
                        ended = False
                        self._settle(pid, Status.WORKING)
                    # A streak of one is undecided: say nothing.
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("observing screen of %s failed", pid)
            await self._sleep(self.screen)

    def _end_turn_from_screen(self, pid: str, capture: str) -> None:
        """Record a turn boundary that was seen rather than read.

        The bus event is marked `source: "screen"` and carries index -1: it did
        not come from any transcript record, and nothing downstream should be
        able to mistake a rendering for a parsed message.
        """
        text = clip(screen_result(capture))
        self.store.bus_append(
            "agent.assistant",
            from_id=pid,
            payload={
                "text": text,
                "tool": None,
                "ts": None,
                "turn_end": True,
                "index": -1,
                "source": "screen",
            },
        )
        self._finish_jobs_for_turn(pid, text)

    async def _capture(self, pane: str) -> str | None:
        """The pane's rendered text, or None if it could not be read."""
        from theater.tmux import client as tmux

        try:
            return await tmux.run("capture-pane", "-p", "-t", pane, check=False)
        except Exception:
            return None

    async def _open(
        self,
        pid: str,
        harness: Harness,
        cursor: TranscriptCursor,
        path: Path | None = None,
    ) -> bool:
        """Point the cursor at the end of a transcript. False if there is none.

        Pass `path` to adopt a known file (a rotation); omit it to go looking.
        """
        if path is None:
            path = await self._locate(pid, harness)
            if path is None:
                return False
        offset, index, mtime, last_line = await asyncio.to_thread(_attach_point, path)
        cursor.attach(path, offset, index, mtime)
        self._on_attach(pid, harness, path, index, last_line)
        return True

    def _unblock(self, pid: str) -> None:
        """New bytes mean the agent is working, whatever the screen said."""
        p = self.store.get_participant(pid)
        if p and p.status is Status.AWAITING_INPUT:
            self.registry.set_status(pid, Status.WORKING)

    async def _on_quiet(
        self, pid: str, harness: Harness, cursor: TranscriptCursor
    ) -> None:
        """Nothing was written this tick. Run the two quiet timers.

        Both read the same silence, and neither may reset the other: see
        TranscriptCursor for what happens when they share a clock.
        """
        now = time.monotonic()
        cursor.begin_quiet(now)

        # Look for a rotation. Vibe opens a new session directory every turn,
        # so a quiet file may just be the wrong file.
        if cursor.quiet_for(now) > self.relocate:
            await self._follow_rotation(pid, harness, cursor)

        # Then ask the screen whether this silence is a prompt waiting on a
        # human rather than an agent thinking.
        if cursor.screen_quiet_for(now) > self.awaiting:
            await self._check_idle_screen(pid, harness)
            cursor.screen_quiet_since = now  # throttle to one check per window

    async def _follow_rotation(
        self, pid: str, harness: Harness, cursor: TranscriptCursor
    ) -> None:
        """Move to the newest transcript if the harness started a new one.

        The same path back means the agent is idle, not rotated. That case
        leaves both timers alone on purpose — the relocate must not reset the
        clock the screen check is reading. It does mean the scan repeats every
        poll while a participant sits idle; a directory listing is cheap enough
        that trading it for a slower rotation pickup is not worth it.
        """
        path = await self._relocate(pid, harness)
        if path is None or path == cursor.path:
            return
        logger.info("transcript for %s rotated: %s -> %s", pid, cursor.path, path)
        await self._open(pid, harness, cursor, path=path)

    async def _locate(self, pid: str, harness: Harness) -> Path | None:
        """Find the transcript by session_id (fast) or cwd scan (fallback)."""
        p = self.store.get_participant(pid)
        if p is None or not p.cwd:
            return None
        after = p.created_at if p.tier is Tier.SPAWNED else None
        return await asyncio.to_thread(
            harness.find_transcript,
            cwd=p.cwd,
            session_id=p.session_id,
            after=after,
        )

    async def _relocate(self, pid: str, harness: Harness) -> Path | None:
        """Find the newest transcript by cwd only, ignoring session_id.

        Vibe starts a new session directory on each turn. The session_id
        stored on the participant pins find_transcript to the FIRST
        session directory, which never grows after the agent rotates.
        Re-locating must scan by cwd only, so the newest matching
        directory is found regardless of the stored session id.
        """
        p = self.store.get_participant(pid)
        if p is None or not p.cwd:
            return None
        after = p.created_at if p.tier is Tier.SPAWNED else None
        return await asyncio.to_thread(
            harness.find_transcript,
            cwd=p.cwd,
            session_id=None,
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

    async def _check_idle_screen(self, pid: str, harness: Harness) -> None:
        """Check the rendered screen for a bare prompt.

        If the transcript says WORKING but the screen shows a bare prompt,
        the agent is blocked on a permission prompt or waiting for input —
        set AWAITING_INPUT. If the screen shows agent output, the agent is
        genuinely working — leave as WORKING. If capture-pane fails, leave
        as WORKING (accept false negatives).
        """
        p = self.store.get_participant(pid)
        if p is None or p.status is not Status.WORKING:
            return  # only check if transcript says WORKING
        if not p.tmux_pane:
            return
        capture = await self._capture(p.tmux_pane)
        if capture is None:
            return
        if harness.is_idle_screen(capture):
            self.registry.set_status(pid, Status.AWAITING_INPUT)
            logger.info("participant %s awaiting input (bare prompt on screen)", pid)

    def _finish_jobs_for_turn(self, pid: str, result_text: str) -> None:
        """Finish any running jobs for this participant with the result text.

        Called when the observer detects a turn_end. The result is the
        assistant's text from the turn-end event — clipped to MAX_TEXT by
        the harness parser already. If the participant has no running jobs
        (e.g. it's a hand-started session nobody spawned), this is a no-op.
        """
        if self.jobs is None:
            return
        running = self.store.running_jobs_for_target(pid)
        for job in running:
            self.jobs.finish(
                job.handle,
                state=JobState.DONE,
                result=result_text or "",
            )