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
Those are two jobs, and only the second one lives here. The first belongs to
the adapter: a `HarnessObserver` (harness/observation.py) opens a `Source`
(harness/source.py) that produces batches of events for one participant, and
every harness that appends JSONL gets the default file-tailing one without
saying so. This module owns what happens next: the quiet timers, the status
policy, job completion and rescue, and every write to the registry and the bus.
An adapter whose output is not a file writes its own `Source` and inherits all
of that unchanged, which is the point — the policy below is where every
observation bug in this project has been, and it is not going to be
reimplemented per adapter.

Note what this module is handed: a `HarnessObserver`, never a `Harness`. It
needs nothing from an adapter but how to watch it, and holding only that half
keeps the launch path from drifting into the observe path.

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
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from theater.config import ObserverSection
from theater.daemon.registry import Registry
from theater.harness import (
    HARNESSES,
    EventKind,
    Harness,
    HarnessObserver,
    clip,
    status_after,
)
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


#: How many handled turn ids one participant remembers. Its only job is to
#: absorb a harness announcing the same boundary twice, and those duplicates
#: are adjacent — Claude writes them as consecutive records of one split
#: message — so a small window is enough. Bounded because a watcher lives as
#: long as its participant does, and an unbounded set here would be a slow
#: leak on a session that runs for a day.
_ANSWERED_TURNS = 32

#: How much of a prompt has to reappear before a turn is called an answer to
#: it. Not the whole prompt: every harness clips what it reports at
#: `harness.MAX_TEXT`, so a long prompt never comes back whole, and a wrapper
#: around it would defeat equality anyway. Long enough to be specific — two
#: prompts sharing 120 characters are the same question by any reading — and
#: short enough that the clip point is nowhere near it.
_PROMPT_MATCH = 120

#: How many consecutive turn ends that do not match the waiting job's prompt
#: are tolerated before the job is released. One is legitimate: a human
#: interjects, and the injected prompt is genuinely still queued behind
#: theirs. Two means the pane processed two other turns while ours supposedly
#: waited, which no real queue does — the prompt was never delivered, and the
#: job would otherwise stay running forever, because the rescue timer cannot
#: fire on a participant that is actively working. The accepted cost: a human
#: taking two turns back to back while our prompt legitimately waits behind
#: them will fail the job early. That is cheaper than an unbounded wedge, and
#: the error code says which happened.
UNMATCHED_LIMIT = 2

#: How many entries the per-job miss counter (`Observer._unmatched`) holds
#: before the oldest is evicted. The dict is cleared on a match and in
#: `_finish`, but a job can end outside the observer — a `kill`, or the
#: `send_failed` path in methods.py — which leaves its entry behind if a miss
#: was already recorded. Unbounded, that is a slow leak on a watcher that
#: lives as long as its participant does, exactly the class of bug
#: `_ANSWERED_TURNS` above was sized for. A plain dict preserves insertion
#: order, so popping the first key is enough to drop the oldest.
UNMATCHED_CAP = 256

#: Set on a job released because its prompt was never seen in the transcript
#: after `UNMATCHED_LIMIT` turn ends answered someone else. The job is
#: finished as CRASHED, not DONE: no prompt landed and no answer exists, which
#: is the same class of failure as a `send` whose `deliver_text` raised.
#: Distinct from `RESCUE_CODE` (a turn end that was never observed at all,
#: which salvages text and stays DONE) so a caller can tell the two apart.
UNDELIVERED_CODE = "prompt_never_seen"


def answers_prompt(heard: Sequence[str], prompt: str | None) -> bool:
    """Did this turn begin with the prompt we injected?

    Every harness Theater drives echoes an injected prompt back as a user
    record before the reply — verified against captures of one real
    round-trip per harness in `tests/test_turn_identity.py`, not assumed. So
    the user text a turn opens with says who the turn belongs to, and a turn
    that opens with something else belongs to whoever typed it.

    Absence of evidence answers yes. A participant we attached to mid-turn, a
    harness that keeps no user record, and the screen-derived boundary of a
    harness with no transcript at all have no user text to offer, and refusing
    to answer there would hang every caller of those. The gate exists to catch
    positive evidence that a turn is *someone else's*, and nothing weaker.

    Matching is a normalised prefix rather than equality, in both directions:
    whitespace survives injection unreliably, the reported text is clipped,
    and a harness is free to wrap the prompt in scaffolding of its own.
    """
    if not prompt or not prompt.strip():
        # A spawn with no prompt has nothing to claim. Answering keeps the
        # documented behaviour where such a job soaks up the next turn.
        return True
    if not heard:
        return True
    needle = " ".join(prompt.split())[:_PROMPT_MATCH]
    return any(needle in " ".join(text.split()) for text in heard)


@dataclass(frozen=True, slots=True)
class Turn:
    """One finished turn: what the agent said, and what it was replying to.

    A record rather than a pair, because both halves are sequences of text and
    a caller that transposes them gets no complaint from the type checker.
    """

    #: Assistant text, blank-line joined in arrival order.
    said: str
    #: User text that arrived during the turn, in arrival order. Normally the
    #: one prompt that opened it; empty when we attached mid-turn.
    heard: tuple[str, ...] = ()


@dataclass
class TurnAccumulator:
    """What one participant has said since its last turn boundary.

    Lives for as long as the watcher does, which is the whole point. The text
    used to be a local rebuilt on every `_apply` call, so a turn whose text
    arrived in one poll and whose boundary arrived in the next answered the
    waiting job with an empty string. It also only ever held the *last*
    assistant fragment, so a Claude reply written as three text blocks came
    back as its final paragraph alone.

    Kept apart from `QuietClock` deliberately: that class is the observer's
    sense of time passing and says so in its own docstring. This is
    conversation state. They have the same lifetime and nothing else in common.
    """

    #: Assistant text seen since the last boundary, in arrival order.
    _blocks: list[str] = field(default_factory=list)
    #: User text seen since the last boundary, in arrival order. Kept for
    #: attribution, not for display: it is how a turn says whose it is.
    _heard: list[str] = field(default_factory=list)
    #: Turn ids already handled, newest last. A deque and a set together: the
    #: set answers the question, the deque decides what to forget.
    _answered: deque[str] = field(default_factory=deque)
    _seen: set[str] = field(default_factory=set)

    def say(self, text: str) -> None:
        if text:
            self._blocks.append(text)

    def hear(self, text: str) -> None:
        if text:
            self._heard.append(text)

    def take(self) -> Turn:
        """The finished turn, and forget it. Text blank-line joined, as written."""
        turn = Turn("\n\n".join(self._blocks), tuple(self._heard))
        self._blocks.clear()
        self._heard.clear()
        return turn

    def already_handled(self, turn_id: str | None) -> bool:
        """Has this exact turn already been dealt with?

        Dealt with, not answered: a turn we deliberately declined to answer
        because it was a human's is handled too. Were it not marked, Claude's
        duplicate boundary would arrive with the accumulator already emptied,
        find no user text, read that as no evidence, and answer after all.

        An unidentified boundary is never a duplicate: a harness that publishes
        no turn id gets one answer per boundary, which is what it had before.
        """
        return turn_id is not None and turn_id in self._seen

    def mark_handled(self, turn_id: str | None) -> None:
        if turn_id is None or turn_id in self._seen:
            return
        self._answered.append(turn_id)
        self._seen.add(turn_id)
        while len(self._answered) > _ANSWERED_TURNS:
            self._seen.discard(self._answered.popleft())


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
        #: Per-job count of consecutive turn ends that did not match the
        #: waiting prompt. Cleared on a match or when the job is finished.
        self._unmatched: dict[str, int] = {}
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
            observer = harness.observer
            # A transcript is found by working directory; a screen is found by
            # pane. So cwd is required for one loop and irrelevant to the other.
            if observer.has_transcript and not p.cwd:
                self._warn_unobservable(pid, p)
                continue
            self._unobservable.discard(pid)
            watch = self._watch if observer.has_transcript else self._watch_screen
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
        # Resolved by name on every start rather than captured once, so a
        # restarted watcher picks up a registry that has since been reinstalled.
        observer = self.harnesses[harness_name].observer
        source = self._open_source(pid, observer)
        if source is None:
            return
        clock = QuietClock()
        turns = TurnAccumulator()

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
                    if self._apply(pid, batch, clock, turns):
                        clock.stir()
                        self._unblock(pid)
                    else:
                        await self._on_quiet(pid, observer, source, clock, turns)
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

    def _open_source(self, pid: str, observer: HarnessObserver) -> Source | None:
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
        return observer.open_source(
            cwd=p.cwd, session_id=p.session_id, after=after
        )

    def _apply(
        self, pid: str, batch: Batch, clock: QuietClock, turns: TurnAccumulator
    ) -> bool:
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

        The turn's text lives in `turns`, which outlives this call. Batches are
        cut wherever the poll happened to land, so a turn is routinely split
        across two of them, and text accumulated in one must still be there
        when the boundary arrives in the next.
        """
        if batch.attached is not None:
            self._on_attach(pid, batch.attached)

        last = None
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
                    "turn": event.turn_id,
                    "index": event.raw_index,
                },
            )
            last = event
            if event.kind is EventKind.ASSISTANT and event.text:
                clock.last_text = event.text
                turns.say(event.text)
            if event.kind is EventKind.USER and event.text:
                turns.hear(event.text)
            if event.turn_end:
                # Whatever the turn said, whether it came in this batch or an
                # earlier one. Always taken, even when the boundary is a
                # duplicate we will not answer: the text belongs to the turn
                # that just ended and must not spill into the next one.
                turn = turns.take()
                if not turns.already_handled(event.turn_id):
                    # The boundary's own text still wins where it has any.
                    # Codex repeats the whole reply on `task_complete` after a
                    # preamble has already gone by, so joining the two would
                    # hand the caller the preamble twice.
                    self._answer_turn(pid, event.text or turn.said, turn.heard)
                    turns.mark_handled(event.turn_id)
                # Rescue salvages `last_text` when no boundary is ever read.
                # Past this one, anything said before it has been delivered
                # and is not a candidate answer to whatever comes next — a
                # rescue returning it would look like a reply rather than the
                # stale echo it is.
                clock.last_text = ""

        if batch.status is not None:
            self._settle(pid, batch.status)
        elif last is not None:
            self._settle(pid, status_after(last))

        return batch.progressed or bool(batch.events) or batch.attached is not None

    async def _watch_screen(self, pid: str, harness_name: str) -> None:
        """Derive status from the rendered screen, for a parser-less harness.

        An observer with `has_transcript = False` has nothing to read, so no
        `turn_end` event can ever be produced. Without one, `theater_send`
        would accept a prompt and leave the caller's `await_sessions` waiting
        forever. So here the idle-prompt heuristic is promoted from a display
        hint to a completion signal.

        That inverts the risk profile documented on `is_idle_screen`, which was
        tuned to accept false negatives: a false idle now finishes a job early
        and hands the caller a partial answer. Two mitigations, both narrow.
        The promotion applies only to observers that can read nothing — never
        to one whose output we can read. And an idle screen must hold for
        `IDLE_CONFIRMATIONS` consecutive polls before it counts.

        An unreadable screen decides nothing. Failing to capture is not
        evidence of either state, so the status is left exactly as it was.
        """
        observer = self.harnesses[harness_name].observer
        idle_streak = 0
        ended = False

        while not self._stopping.is_set():
            try:
                p = self.store.get_participant(pid)
                if p is None or p.status is Status.DEAD:
                    return
                capture = await self._capture(p.tmux_pane) if p.tmux_pane else None
                if capture is not None:
                    idle_streak = (
                        idle_streak + 1 if observer.is_idle_screen(capture) else 0
                    )
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
        self,
        pid: str,
        observer: HarnessObserver,
        source: Source,
        clock: QuietClock,
        turns: TurnAccumulator,
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
            if self._apply(pid, batch, clock, turns):
                clock.stir()
                return

        # Then ask the screen whether this silence is a prompt waiting on a
        # human rather than an agent thinking.
        if clock.screen_quiet_for(now) > self.awaiting:
            await self._check_idle_screen(pid, observer)
            clock.screen_quiet_since = now  # throttle to one check per window

        # Finally, much later, assume a turn end we never read and release
        # anyone still waiting on this participant.
        if clock.rescue_quiet_for(now) > self.rescue:
            await self._rescue_jobs(pid, observer, clock)
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

    async def _check_idle_screen(self, pid: str, observer: HarnessObserver) -> None:
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
        if observer.is_idle_screen(capture):
            self.registry.set_status(pid, Status.AWAITING_INPUT)
            logger.info("participant %s awaiting input (bare prompt on screen)", pid)

    async def _rescue_jobs(
        self, pid: str, observer: HarnessObserver, clock: QuietClock
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
        if capture is None or not observer.is_idle_screen(capture):
            return
        logger.warning(
            "no turn end seen for %s after %.0fs of quiet; finishing its jobs",
            pid,
            self.rescue,
        )
        self._release_jobs(pid, clock.last_text, error_code=RESCUE_CODE)

    def _answer_turn(
        self, pid: str, result_text: str, heard: Sequence[str] = ()
    ) -> None:
        """One turn ended: hand its text to the one job that was waiting for it.

        The oldest running job, and only that one. Prompts arrive at a pane in
        the order they were typed and the agent works through them in that
        order, so turn N answers prompt N. Resolving every running job at each
        boundary — which is what this used to do — gave a queued second caller
        the reply to the first caller's question, and did it instantly, before
        its prompt had been read.

        `heard` is what the turn was replying to, and it is checked against the
        job's prompt before the job is resolved. Position alone was not enough:
        a human typing into a pane that has a job waiting produces a perfectly
        ordinary turn end, and the peer got the operator's conversation as the
        answer to its question. Both halves of that are bad — a wrong answer,
        and the operator's private text handed to another agent.

        A turn that does not answer the waiting job leaves it running, up to
        `UNMATCHED_LIMIT` consecutive misses. One unmatched turn is legitimate
        — a human interjects, and the injected prompt is genuinely still queued
        behind theirs — so the job survives a single miss and the next turn may
        answer it. Two consecutive misses mean the pane processed two other
        turns while ours supposedly waited, which no real queue does: the
        prompt was never delivered (a human at the pane can clear the composer
        before it is read), and the job is released as CRASHED with
        `UNDELIVERED_CODE` rather than left running indefinitely. CRASHED, not
        DONE, because no prompt landed and no answer exists — the same state
        `send` itself uses when `deliver_text` fails. The rescue timer cannot
        save this case, because it requires an idle screen and the participant
        is actively working. The accepted cost: a human taking two turns back
        to back while our prompt legitimately waits behind them will fail the
        job early, which is cheaper than an unbounded wedge.

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
        if job is None:
            return
        if not answers_prompt(heard, job.prompt):
            missed = self._unmatched.get(job.handle, 0) + 1
            self._unmatched[job.handle] = missed
            while len(self._unmatched) > UNMATCHED_CAP:
                self._unmatched.pop(next(iter(self._unmatched)))
            if missed < UNMATCHED_LIMIT:
                logger.info(
                    "turn at %s replies to something else; %s keeps waiting",
                    pid,
                    job.handle,
                )
                return
            logger.warning(
                "%s saw %d turns at %s answer someone else; "
                "its prompt never reached the queue",
                job.handle,
                missed,
                pid,
            )
            self._finish(
                job.handle, "", error_code=UNDELIVERED_CODE, state=JobState.CRASHED
            )
            return
        self._unmatched.pop(job.handle, None)
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
        self,
        handle: str,
        result_text: str,
        *,
        error_code: str | None = None,
        state: JobState = JobState.DONE,
    ) -> None:
        """Resolve one job. The result is already clipped by the parser.

        `error_code` is set only by the rescue and undelivered paths. The two
        differ in state because they differ in what the caller actually has.

        Rescue salvages the last text the agent said before the quiet window:
        not a declared reply, but a real answer the caller can read. Reporting
        DONE there is deliberate — the caller has a usable result, and blocking
        on a CRASHED job would defeat the point of rescuing it.

        Undelivered has no answer at all. The prompt never reached the queue,
        nothing was said in reply to it, and the empty string passed in here is
        a placeholder rather than a result. Reporting DONE would make that read
        as "the peer replied with nothing", which is a different failure than
        the one that happened. CRASHED says what is true: no prompt landed, no
        answer exists. That is the same state `send` itself uses when
        `deliver_text` fails before the prompt is typed — the same class of
        failure, one tick later.
        """
        assert self.jobs is not None
        self._unmatched.pop(handle, None)
        self.jobs.finish(
            handle,
            state=state,
            result=result_text or "",
            error_code=error_code,
        )
