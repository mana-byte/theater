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

Getting the text, and deciding what it means
--------------------------------------------
Those are two jobs, and only the second one lives here. A `Source` — see
harness/source.py — produces batches of events for one participant, and every
harness that appends JSONL gets the default file-tailing one without saying so.
This module owns what happens next: the quiet timers, the status policy, job
completion and rescue, and every write to the registry and the bus. A harness
whose output is not a file overrides `open_source` and inherits all of that
unchanged, which is the point — the policy below is where every observation bug
in this project has been, and it is not going to be reimplemented per adapter.

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
import time
from dataclasses import dataclass

from theater.config import ObserverSection
from theater.daemon.registry import Registry
from theater.harness import HARNESSES, EventKind, Harness, clip, status_after
from theater.harness.source import Attachment, Batch, Source
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
RESCUE_TIMEOUT = _DEFAULTS.rescue_timeout

#: Marks a job the observer finished without ever reading a turn-end record.
#: The caller gets the last thing the agent said, and this code to say that is
#: what it is: a salvage, not a reply the harness declared complete.
RESCUE_CODE = "turn_end_unseen"

#: Consecutive idle-looking screens before a turn is called finished. Two, not
#: one: the screen is a rendering, and a harness that clears the pane between
#: phases shows a bare prompt for one frame in the middle of working. Finishing
#: a job there hands the caller a partial answer, which is worse than being a
#: poll late — see `_watch_screen`.
IDLE_CONFIRMATIONS = 2


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
class QuietClock:
    """How long one participant's watcher has gone without hearing anything.

    Three quiet timers, not one. They measure the same silence but are reset by
    different events, and collapsing them into a single timer is a bug we have
    already shipped once: the relocate fires at 5s and used to reset the clock,
    so the 10s screen check was never reached and AWAITING_INPUT never appeared.
    The rescue timer has the same problem in a worse form — the screen check
    throttles itself by pushing its own clock forward every time it fires, so a
    rescue reading that clock would never come due at all.

    Where the watcher has read *to* is not here any more; that belongs to the
    source, which is the only thing that knows what a position even means for
    its input. This is purely the observer's sense of time passing.
    """

    #: When the participant went quiet, for the relocate timer.
    quiet_since: float | None = None
    #: The same silence, for the screen check. Reset independently.
    screen_quiet_since: float | None = None
    #: The same silence again, for the job rescue. Reset independently.
    rescue_since: float | None = None
    #: The last thing the agent was heard to say. What a rescued job returns,
    #: since by definition no turn-end event arrived to carry a result.
    last_text: str = ""

    def stir(self) -> None:
        """Something happened: every timer starts counting again from zero."""
        self.quiet_since = None
        self.screen_quiet_since = None
        self.rescue_since = None

    def begin_quiet(self, now: float) -> None:
        """Start whichever timers are not already running."""
        if self.quiet_since is None:
            self.quiet_since = now
        if self.screen_quiet_since is None:
            self.screen_quiet_since = now
        if self.rescue_since is None:
            self.rescue_since = now

    def quiet_for(self, now: float) -> float:
        return now - (self.quiet_since if self.quiet_since is not None else now)

    def screen_quiet_for(self, now: float) -> float:
        since = self.screen_quiet_since
        return now - (since if since is not None else now)

    def rescue_quiet_for(self, now: float) -> float:
        since = self.rescue_since
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
        rescue: float = RESCUE_TIMEOUT,
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
        self.rescue = rescue
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
        source = self._open_source(pid, harness)
        if source is None:
            return
        clock = QuietClock()

        try:
            while not self._stopping.is_set():
                try:
                    batch = await source.read()
                    if batch.waiting:
                        # Nothing to read from yet. Back off on the search
                        # interval and start no timers: silence from a source
                        # that has not attached says nothing about the agent.
                        await self._sleep(self.search)
                        continue
                    if self._apply(pid, batch, clock):
                        clock.stir()
                        self._unblock(pid)
                    else:
                        await self._on_quiet(pid, harness, source, clock)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("observing %s failed", pid)
                await self._sleep(self.poll)
        finally:
            try:
                await source.aclose()
            except (Exception, asyncio.CancelledError):
                # Closing runs while unwinding, so it may itself be cancelled.
                # Swallowing that here does not lose the cancellation: the
                # exception that brought us into `finally` still propagates.
                logger.debug("closing source for %s failed", pid, exc_info=True)

    def _open_source(self, pid: str, harness: Harness) -> Source | None:
        """Build the source for a participant, from what the registry knows.

        Read once, at watcher start. cwd cannot change during a participant's
        life, and `after` is a floor on start time that only applies to
        participants we spawned — an adopted session's output predates our
        first sight of it.
        """
        p = self.store.get_participant(pid)
        if p is None:
            return None
        after = p.created_at if p.tier is Tier.SPAWNED else None
        return harness.open_source(cwd=p.cwd, session_id=p.session_id, after=after)

    def _apply(self, pid: str, batch: Batch, clock: QuietClock) -> bool:
        """Put a batch on the bus and move the participant's status.

        Returns whether anything happened, which is what the quiet timers read.
        A source that consumed input says so with `progressed`; events and a
        fresh attachment count too, so an adapter that reports one without the
        other is not punished for it.

        `batch.status` wins over the status implied by the last event. That is
        the whole of the authoritative-status channel: a source that can ask
        the harness directly is believed, and one that cannot stays silent and
        gets the inference below.

        Turn ends are answered *inside* the loop, at every boundary. A poll
        drains everything written since the last one, so one batch routinely
        holds a whole turn plus the beginning of the next: `[assistant(end),
        user]`. Inspecting only the final event missed that boundary entirely
        and left the caller waiting for the rescue timer — the long-standing
        "it always breaks on the second reply". Two boundaries in one batch
        used to collapse into one for the same reason.
        """
        if batch.attached is not None:
            self._on_attach(pid, batch.attached)

        last = None
        # Assistant text seen since the previous boundary, so a turn end that
        # carries none of its own still answers with what the turn said.
        # Codex marks the boundary on `task_complete`, an event with no text.
        reply = ""
        for event in batch.events:
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
            if event.kind is EventKind.ASSISTANT and event.text:
                clock.last_text = event.text
                reply = event.text
            if event.turn_end:
                self._answer_turn(pid, event.text or reply)
                reply = ""

        if batch.status is not None:
            self._settle(pid, batch.status)
        elif last is not None:
            self._settle(pid, status_after(last))

        return batch.progressed or bool(batch.events) or batch.attached is not None

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
        self._answer_turn(pid, text)

    async def _capture(self, pane: str) -> str | None:
        """The pane's rendered text, or None if it could not be read."""
        from theater.tmux import client as tmux

        try:
            return await tmux.run("capture-pane", "-p", "-t", pane, check=False)
        except Exception:
            return None

    def _unblock(self, pid: str) -> None:
        """New output means the agent is working, whatever the screen said."""
        p = self.store.get_participant(pid)
        if p and p.status is Status.AWAITING_INPUT:
            self.registry.set_status(pid, Status.WORKING)

    async def _on_quiet(
        self, pid: str, harness: Harness, source: Source, clock: QuietClock
    ) -> None:
        """Nothing arrived this tick. Run the three quiet timers.

        All read the same silence, and none may reset another: see
        QuietClock for what happens when they share a clock.
        """
        now = time.monotonic()
        clock.begin_quiet(now)

        # Ask the source whether it should be reading somewhere else. Vibe
        # opens a new session directory every turn, so a quiet transcript may
        # just be the wrong transcript. Not done every poll: for a file-backed
        # source this is a directory scan.
        if clock.quiet_for(now) > self.relocate:
            batch = await source.refresh()
            if self._apply(pid, batch, clock):
                clock.stir()
                return

        # Then ask the screen whether this silence is a prompt waiting on a
        # human rather than an agent thinking.
        if clock.screen_quiet_for(now) > self.awaiting:
            await self._check_idle_screen(pid, harness)
            clock.screen_quiet_since = now  # throttle to one check per window

        # Finally, much later, assume a turn end we never read and release
        # anyone still waiting on this participant.
        if clock.rescue_quiet_for(now) > self.rescue:
            await self._rescue_jobs(pid, harness, clock)
            clock.rescue_since = now  # throttle, same as above

    def _on_attach(self, pid: str, attached: Attachment) -> None:
        """A source started reading somewhere. Say so, and settle the status."""
        p = self.store.get_participant(pid)
        session_id = attached.session_id
        if p is not None and session_id and p.session_id != session_id:
            p.session_id = session_id
            self.store.upsert_participant(p)
        self.store.bus_append(
            "agent.transcript",
            to_id=pid,
            payload={"path": attached.location, "skipped_records": attached.skipped},
        )
        logger.info(
            "observing %s at %s (+%d existing records)",
            pid,
            attached.location,
            attached.skipped,
        )
        # Derive an initial status from the last record skipped, so a spawned
        # agent that finished its turn before we attached does not stay
        # STARTING forever. The bus gets no history replayed — only the status
        # moves. A source with nothing behind it leaves the status as it was.
        event = attached.last_event
        if event is not None:
            self._settle(pid, status_after(event))
            if event.turn_end:
                self._answer_turn(pid, event.text)

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

    async def _rescue_jobs(
        self, pid: str, harness: Harness, clock: QuietClock
    ) -> None:
        """Finish a job whose turn end was never read, so the caller unblocks.

        A missed turn-end record is not hypothetical: a harness can abort a
        turn, rotate its transcript at the wrong moment, or write a boundary
        this parser does not recognise. Whatever the cause, the caller's
        `await_sessions` waits on a promise nothing will ever resolve, and the
        symptom the user sees is a conversation that dies on the second reply.

        So: a long silence, over a screen that looks idle, with jobs still
        running, is taken as a turn that ended unobserved. The caller gets the
        last thing the agent said and `RESCUE_CODE`, which says plainly that
        this is salvage rather than a declared reply.

        Deliberately narrow. An unreadable screen decides nothing — the same
        rule `_watch_screen` follows — and a participant with no pane cannot
        be rescued at all. Status is left alone: `_check_idle_screen` has
        already had its say at a much shorter timeout, and this is about the
        promise, not the participant.
        """
        if self.jobs is None or not self.store.running_jobs_for_target(pid):
            return
        p = self.store.get_participant(pid)
        if p is None or not p.tmux_pane:
            return
        capture = await self._capture(p.tmux_pane)
        if capture is None or not harness.is_idle_screen(capture):
            return
        logger.warning(
            "no turn end seen for %s after %.0fs of quiet; finishing its jobs",
            pid,
            self.rescue,
        )
        self._release_jobs(pid, clock.last_text, error_code=RESCUE_CODE)

    def _answer_turn(self, pid: str, result_text: str) -> None:
        """One turn ended: hand its text to the one job that was waiting for it.

        The oldest running job, and only that one. Prompts arrive at a pane in
        the order they were typed and the agent works through them in that
        order, so turn N answers prompt N. Resolving every running job at each
        boundary — which is what this used to do — gave a queued second caller
        the reply to the first caller's question, and did it instantly, before
        its prompt had been read.

        A participant with nothing running is the normal case for a session
        nobody sent to: no-op.

        Note the queue this trusts is not entirely ours. A CLI spawn creates a
        job nobody awaits, and it legitimately takes the first turn end,
        because the spawn prompt *is* the first turn. Spawning with an empty
        prompt leaves that job to soak up the next turn instead — an
        off-by-one bounded to a single job, and preferable to the alternative
        of answering the wrong caller.
        """
        if self.jobs is None:
            return
        job = self.store.oldest_running_job_for_target(pid)
        if job is not None:
            self._finish(job.handle, result_text)

    def _release_jobs(
        self, pid: str, result_text: str, *, error_code: str | None = None
    ) -> None:
        """Finish *every* running job for this participant. Rescue only.

        The counterpart to `_answer_turn`, and the reason the two are separate
        methods rather than one with a flag. Rescue fires when no turn end was
        ever observed, which means the per-turn accounting has already failed;
        there is no boundary left to match a job to, and nothing else will come
        along to release the rest. Leaving all but one waiting until each
        rescue window elapses would drip them out over minutes.
        """
        if self.jobs is None:
            return
        for job in self.store.running_jobs_for_target(pid):
            self._finish(job.handle, result_text, error_code=error_code)

    def _finish(
        self, handle: str, result_text: str, *, error_code: str | None = None
    ) -> None:
        """Resolve one job. The result is already clipped by the parser.

        `error_code` is set only by the rescue path. The state stays DONE
        either way: the caller has a usable answer and blocking on a FAILED
        job would defeat the point of rescuing it.
        """
        assert self.jobs is not None
        self.jobs.finish(
            handle,
            state=JobState.DONE,
            result=result_text or "",
            error_code=error_code,
        )