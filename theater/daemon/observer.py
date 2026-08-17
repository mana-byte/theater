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

What the screen adds
---------------------
IDLE and WORKING are derivable from the transcript alone; AWAITING_INPUT is
not, because a permission modal and a thinking agent produce the same silence.
`_check_idle_screen` reads the rendered screen via the harness's
`screen_reading` and maps it to a status: APPROVAL/TRUST settles
AWAITING_INPUT, WORKING settles WORKING, PROMPT settles IDLE, and UNKNOWN
leaves the status untouched. The reducer acts on this reading regardless of
confidence — being wrong here costs a mislabel in the display, which is
cheaper than the unrecoverable cost a send gate would pay for the same
mistake, so the two consumers use different confidence thresholds.

That check runs on both paths through the watch loop, and it has to. A source
that has not attached yet reports `waiting` rather than silence, and the
waiting path used to skip every timer — so a harness that writes no transcript
until its first message (Claude) had no status channel at all before it was
first prompted, and sat at the IDLE its spawn set. `_screen_only` runs the
screen arm there, and only that arm; see its docstring for why the other two
are not merely unnecessary but wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from theater.config import ObserverSection
from theater.daemon.registry import Registry
from theater.harness import (
    HARNESSES,
    EventKind,
    Harness,
    HarnessObserver,
    ScreenKind,
    clip,
    status_after,
)
from theater.harness.observation import open_participant_source
from theater.harness.source import Attachment, Batch, History, Source, SourceContractError
from theater.models import JobState, Status, Tier
from theater.models import now as wall_now
from theater.provenance import (
    TranscriptProvenance,
    is_trusted_provenance,
    normalize_provenance,
    provenance_at_least,
)

logger = logging.getLogger("theater.observer")

#: Fallback timings. `config.ObserverSection` owns the literal so the
#: default and the settable value cannot drift. Tests use these directly.
_DEFAULTS = ObserverSection()

POLL_INTERVAL = _DEFAULTS.poll_interval
RELOCATE_TIMEOUT = _DEFAULTS.relocate_timeout
AWAITING_INPUT_TIMEOUT = _DEFAULTS.awaiting_input_timeout
SEARCH_INTERVAL = _DEFAULTS.search_interval
SYNC_INTERVAL = _DEFAULTS.sync_interval
SCREEN_INTERVAL = _DEFAULTS.screen_interval
RESCUE_TIMEOUT = _DEFAULTS.rescue_timeout

#: Marks a job the observer finished without ever reading a turn-end record.
#: Salvage, not a reply the harness declared complete.
RESCUE_CODE = "turn_end_unseen"

#: Consecutive idle-looking screens before a turn is called finished. Two, not
#: one: a harness that clears the pane between phases shows a bare prompt for
#: one frame mid-work. Finishing there hands the caller a partial answer.
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
        lines.pop()
    return "\n".join(lines).strip()


#: How many handled turn ids one participant remembers. Duplicates are
#: adjacent — Claude writes them as consecutive records — so a small window
#: suffices. Bounded because a watcher lives as long as its participant, and
#: an unbounded set would leak on a day-long session.
_ANSWERED_TURNS = 32

#: How much of a prompt has to reappear before a turn is called an answer to
#: it. Not the whole prompt: every harness clips at `harness.MAX_TEXT`, so a
#: long prompt never comes back whole. Long enough to be specific — two
#: prompts sharing 120 characters are the same question — and short enough
#: that the clip point is nowhere near it.
_PROMPT_MATCH = 120

#: How many consecutive turn ends that do not match the waiting job's prompt
#: are tolerated before the job is released. One is legitimate: a human
#: interjects, and the injected prompt is genuinely still queued behind
#: theirs. Two means the pane processed two other turns while ours supposedly
#: waited — the prompt was never delivered, and the job would stay running
#: forever, because rescue cannot fire on a participant that is actively
#: working. The cost: a human taking two turns back to back while our prompt
#: legitimately waits will fail the job early — cheaper than an unbounded wedge.
UNMATCHED_LIMIT = 2

#: How many entries the per-job miss counter (`Observer._unmatched`) holds
#: before the oldest is evicted. A job can end outside the observer — a
#: `kill`, or `send_failed` — leaving its entry behind. Unbounded, that is a
#: slow leak on a watcher that lives as long as its participant. A plain dict
#: preserves insertion order, so popping the first key drops the oldest.
UNMATCHED_CAP = 256

#: Set on a job released because its prompt was never seen in the transcript
#: after `UNMATCHED_LIMIT` turn ends answered someone else. Finished as
#: CRASHED: no prompt landed and no answer exists — the same class of failure
#: as a `send` whose `deliver_text` raised. Distinct from `RESCUE_CODE`
#: (salvages text, stays DONE) so a caller can tell the two apart.
UNDELIVERED_CODE = "prompt_never_seen"
OBSERVATION_FAILURE_GRACE = 30.0
CORRELATION_AMBIGUOUS_CODE = "transcript_correlation_ambiguous"
_RAW_RESULT_UNSET = object()


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
        # No prompt to claim; answer yes so the job soaks up the next turn.
        return True
    if not heard:
        return True
    needle = " ".join(prompt.split())[:_PROMPT_MATCH]
    return any(needle in " ".join(text.split()) for text in heard)


def history_correlation_is_ambiguous(registry: Registry, pid: str, history: History) -> bool:
    """Whether a history read could belong to another retained participant.

    History is not a live control decision: dead rows matter too, because their
    transcript files remain on disk. A reducer-accepted pin prevents rescanning
    but does not become exact evidence: duplicate pins and post-epoch missing
    pins still refuse. Pre-epoch NULLs are an explicit compatibility allowance
    for installations where Theater had not begun recording locations yet.
    """
    if is_trusted_provenance(history.correlation):
        return False
    if history.location is None:
        # Nothing can be misattributed when no content was found. Callers
        # report the sharper "transcript missing" diagnostic themselves.
        return False
    participant = registry.store.get_participant(pid)
    if participant is None or not participant.cwd:
        return False
    cwd = Path(participant.cwd).resolve()
    domain = history.collision_domain or participant.transcript_domain
    raw_epoch = registry.store.get_meta("transcript_location_epoch")
    try:
        location_epoch = float(raw_epoch) if raw_epoch is not None else None
    except ValueError:
        location_epoch = None
    for other in registry.list(include_dead=True):
        if (
            other.id == pid
            or other.harness != participant.harness
            or not other.cwd
            or Path(other.cwd).resolve() != cwd
        ):
            continue
        if (
            domain is not None
            and other.transcript_domain is not None
            and domain != other.transcript_domain
        ):
            continue
        if other.transcript_location is not None:
            if history.location is not None and other.transcript_location != history.location:
                continue
            return True
        if (
            other.status is Status.DEAD
            and location_epoch is not None
            and other.last_activity < location_epoch
        ):
            # This row predates location collection. Permitting it is a bounded
            # upgrade-policy choice, not evidence that it owned no transcript.
            continue
        return True
    return False


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
    #: Assistant text before parser clipping, blank-line joined in arrival order.
    raw_said: str = ""


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
    #: Assistant text before clipping, in arrival order.
    _raw_blocks: list[str] = field(default_factory=list)
    #: User text seen since the last boundary. Kept for attribution — how a
    #: turn says whose it is — not for display.
    _heard: list[str] = field(default_factory=list)
    #: Turn ids already handled, newest last. A deque and a set together: the
    #: set answers the question, the deque decides what to forget.
    _answered: deque[str] = field(default_factory=deque)
    _seen: set[str] = field(default_factory=set)

    def say(self, text: str, raw_text: str | None = None) -> None:
        if text or raw_text:
            self._blocks.append(text)
            self._raw_blocks.append(raw_text if raw_text is not None else text)

    def hear(self, text: str) -> None:
        if text:
            self._heard.append(text)

    def take(self) -> Turn:
        """The finished turn, and forget it. Text blank-line joined, as written."""
        turn = Turn(
            said="\n\n".join(self._blocks),
            heard=tuple(self._heard),
            raw_said="\n\n".join(self._raw_blocks),
        )
        self._blocks.clear()
        self._raw_blocks.clear()
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
    #: Last thing the agent said. What a rescued job returns, since no
    #: turn-end event arrived to carry a result.
    last_text: str = ""

    def stir(self) -> None:
        """Semantic output arrived: every timer starts again from zero."""
        self.quiet_since = None
        self.screen_quiet_since = None
        self.rescue_since = None

    def stir_raw(self) -> None:
        """Input was consumed but produced no event or authoritative status.

        Bytes prove the current source is alive, so relocation and rescue must
        both restart. They say nothing about the participant's rendered state:
        an adapter may be consuming a new record shape it cannot parse, and
        resetting the screen clock here would blind the independent fallback
        for as long as those records keep arriving.
        """
        self.quiet_since = None
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
        #: Empty map is legitimate — "observe nothing" — which socket-level
        #: tests want, since real harness roots point at ~/.claude and ~/.vibe.
        self.harnesses = HARNESSES if harnesses is None else harnesses
        self.poll = poll
        self.search = search
        self.sync = sync
        self.relocate = relocate
        self.awaiting = awaiting
        self.screen = screen
        self.rescue = rescue
        #: When set, turn-end events for a participant with a running job
        #: finish that job with the assistant text as the result.
        self.jobs = jobs
        self._tasks: dict[str, asyncio.Task] = {}
        #: Participants whose watcher ended by itself. Not restarted: whatever
        #: stopped it will stop it again.
        self._retired: set[str] = set()
        #: Participants we cannot observe, warned about once each. `hello`
        #: accepts any harness string, so a misreported harness would otherwise
        #: be invisible with nothing saying so.
        self._unobservable: set[str] = set()
        #: Per-job count of consecutive turn ends that did not match the
        #: waiting prompt.
        self._unmatched: dict[str, int] = {}
        #: First wall-clock occurrence of each still-persistent source failure.
        #: A clean batch clears it. Grace is measured from this point and from
        #: each affected job's own creation, whichever is later.
        self._source_errors: dict[tuple[str, str], float] = {}
        #: Transcript path -> participant id, for the live-binding guarantee.
        #: A transcript already bound to one live participant is not silently
        #: rebound to another: two same-cwd siblings can otherwise collapse onto
        #: one file, and the observer attributes one child's turns, status and
        #: bus events to its sibling. Checked in `_accept_attachment`; cleaned up in the
        #: `_watch` finally block.
        self._bound_transcripts: dict[str, str] = {}
        self._binding_correlation: dict[str, str] = {}
        self._binding_sessions: dict[str, str | None] = {}
        self._sources: dict[str, Source] = {}
        self._reset_watch_state: set[str] = set()
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
            # A transcript is found by cwd; a screen by pane. So cwd is
            # required for one loop and irrelevant to the other.
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
        # Resolved by name on every start so a restarted watcher picks up a
        # registry that has since been reinstalled.
        observer = self.harnesses[harness_name].observer
        source = self._open_source(pid, observer)
        if source is None:
            return
        self._sources[pid] = source
        clock = QuietClock()
        turns = TurnAccumulator()

        try:
            while not self._stopping.is_set():
                try:
                    if pid in self._reset_watch_state:
                        self._reset_watch_state.discard(pid)
                        clock = QuietClock()
                        turns = TurnAccumulator()
                    batch = await source.read()
                    self._validate_batch(source, batch)
                    if batch.waiting:
                        self._update_source_error(pid, batch)
                        # Nothing to read from yet. Silence from an unattached
                        # source says nothing about the agent — but the screen
                        # does, and the pane has existed since the spawn. Claude
                        # writes its transcript only on first message, so a pane
                        # parked on the trust dialog has no transcript and no
                        # status without the screen arm.
                        await self._screen_only(pid, observer, clock)
                        await self._sleep(self.search)
                        continue
                    self._report_source_error(pid, batch)
                    if not self._accept_attachment(pid, source, batch):
                        # An initial candidate has nowhere accepted to keep
                        # watching, so stay on the slower discovery backoff.
                        # The rejected transcript says nothing about status,
                        # but the pane still does: without this screen arm a
                        # same-cwd Codex participant remains IDLE throughout a
                        # visible turn merely because its transcript ownership
                        # failed closed.
                        await self._screen_only(pid, observer, clock)
                        await self._sleep(self.search)
                        continue
                    self._clear_source_error_on_progress(pid, batch)
                    if self._apply(pid, batch, clock, turns):
                        self._unblock_on_semantic_progress(pid, batch)
                        await self._on_progress(pid, observer, batch, clock)
                    else:
                        await self._on_quiet(pid, observer, source, clock, turns)
                except asyncio.CancelledError:
                    raise
                except SourceContractError:
                    # Retrying cannot repair an adapter that does not implement
                    # the attachment protocol. Stop this watcher instead of
                    # logging the same traceback at poll cadence forever.
                    logger.exception("source contract failed for %s; retiring watcher", pid)
                    return
                except Exception:
                    logger.exception("observing %s failed", pid)
                await self._sleep(self.poll)
        finally:
            self._clear_source_errors(pid)
            self._reset_watch_state.discard(pid)
            self._sources.pop(pid, None)
            self._release_transcript(pid)
            try:
                await source.aclose()
            except (Exception, asyncio.CancelledError):
                # Closing may itself be cancelled; the exception that brought
                # us into `finally` still propagates.
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
        source = open_participant_source(
            observer,
            participant_id=p.id,
            cwd=p.cwd,
            session_id=p.session_id,
            after=after,
            session_provenance=normalize_provenance(p.session_correlation),
            known_location=p.transcript_location,
            pane_pid=p.live_pid,
        )
        if source.collision_domain is not None and p.transcript_domain != source.collision_domain:
            p.transcript_domain = source.collision_domain
            self.store.upsert_participant(p)
        return source

    @staticmethod
    def _validate_batch(source: Source, batch: Batch) -> None:
        """Reject contradictory source facts without stranding a candidate."""
        if not (batch.waiting and batch.attached is not None):
            return
        source.discard_attachment()
        raise SourceContractError(
            f"{type(source).__name__} returned a batch that is both waiting and attached"
        )

    def _apply(self, pid: str, batch: Batch, clock: QuietClock, turns: TurnAccumulator) -> bool:
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
        # Resolved lazily: the job handle is looked up at most once per
        # _apply call, and only when at least one event carries paths. Most
        # batches carry none, so this defers the database query to the
        # minority where it can pay off. A per-event lookup would run
        # oldest_running_job_for_target on every event of every poll.
        job_handle: str | None = None

        last = None
        for event in batch.events:
            self.store.bus_append(
                f"agent.{event.kind}",
                from_id=pid,
                payload={
                    "text": event.text,
                    "tool": event.tool_name,
                    # The harness's own clock, null when it keeps none. The
                    # bus row's ts is observation time — do not conflate them.
                    "ts": event.ts,
                    "turn_end": event.turn_end,
                    "turn": event.turn_id,
                    "index": event.raw_index,
                },
            )
            last = event
            if event.paths:
                if self.jobs is not None and job_handle is None:
                    job = self.store.oldest_running_job_for_target(pid)
                    job_handle = job.handle if job is not None else ""
                if job_handle:
                    self.jobs.observe_paths(job_handle, event.paths)
            if event.kind is EventKind.ASSISTANT and event.text:
                clock.last_text = event.text
                turns.say(event.text, raw_text=event.raw_text)
            if event.kind is EventKind.USER and event.text:
                turns.hear(event.text)
            if event.turn_end:
                # Whatever the turn said, whether it came in this batch or an
                # earlier one. Always taken, even for a duplicate boundary we
                # will not answer: text must not spill into the next turn.
                turn = turns.take()
                if not turns.already_handled(event.turn_id):
                    # The boundary's own text wins where it has any. Codex
                    # repeats the whole reply on `task_complete` after a
                    # preamble has already gone by; joining would duplicate.
                    result_text, raw_result = self._turn_result(event, turn)
                    self._answer_turn(
                        pid,
                        result_text,
                        turn.heard,
                        raw_result=raw_result,
                    )
                    turns.mark_handled(event.turn_id)
                # Past this turn end, delivered text is not a candidate answer
                # to whatever comes next — rescue returning it would look like
                # a reply rather than a stale echo.
                clock.last_text = ""

        if batch.status is not None:
            self._settle(pid, batch.status)
        elif last is not None:
            self._settle(pid, status_after(last))

        return batch.progressed or bool(batch.events) or batch.attached is not None

    @staticmethod
    def _has_semantic_progress(batch: Batch) -> bool:
        """Whether a batch says something about the participant, not just its source.

        ``progressed`` deliberately includes bookkeeping and unknown records so
        they protect a live turn from relocation and rescue. Events, explicit
        status and attachment evidence are the subset that can also restart the
        independent screen-status clock. Derived here rather than declared by a
        plugin, so a batch cannot contradict its own contents.
        """
        return bool(batch.events) or batch.status is not None or batch.attached is not None

    def _unblock_on_semantic_progress(self, pid: str, batch: Batch) -> None:
        """Raw bookkeeping cannot overrule a modal found by the screen arm."""
        if self._has_semantic_progress(batch):
            self._unblock(pid)

    async def _on_progress(
        self,
        pid: str,
        observer: HarnessObserver,
        batch: Batch,
        clock: QuietClock,
    ) -> None:
        """Reset only the clocks justified by this batch's evidence."""
        if self._has_semantic_progress(batch):
            clock.stir()
            return
        clock.stir_raw()
        await self._screen_status_due(pid, observer, clock)

    def _handle_source_error(self, pid: str, batch: Batch) -> None:
        """Report broken exact correlation and bound affected awaits.

        The source keeps polling, so a late receipt can recover. An old job is
        crashed explicitly rather than waiting forever or falling back to a
        same-cwd transcript that may belong to another process.
        """
        assert batch.error_code is not None
        key = (pid, batch.error_code)
        for stale in [item for item in self._source_errors if item[0] == pid and item != key]:
            self._source_errors.pop(stale, None)
        failed_at = self._source_errors.get(key)
        if failed_at is None:
            failed_at = wall_now()
            self._source_errors[key] = failed_at
            logger.error("observation failed for %s: %s", pid, batch.error or batch.error_code)
            self.store.bus_append(
                "agent.observation_error",
                to_id=pid,
                payload={"code": batch.error_code, "message": batch.error or ""},
            )
        if self.jobs is None:
            return
        now = wall_now()
        for job in self.store.running_jobs_for_target(pid):
            # A source that failed long ago must not instantly crash a prompt
            # just created against a still-live pane. Give both the channel and
            # the individual job a full chance to recover.
            if now - max(failed_at, job.created_at) >= OBSERVATION_FAILURE_GRACE:
                self._finish(
                    job.handle,
                    "",
                    error_code=batch.error_code,
                    state=JobState.CRASHED,
                    raw_result=None,
                )

    def _update_source_error(self, pid: str, batch: Batch) -> None:
        if batch.error_code is None:
            self._clear_source_errors(pid)
            return
        self._handle_source_error(pid, batch)

    def _report_source_error(self, pid: str, batch: Batch) -> None:
        if batch.error_code is not None:
            self._handle_source_error(pid, batch)

    def _clear_source_error_on_progress(self, pid: str, batch: Batch) -> None:
        if batch.error_code is None:
            self._clear_source_errors(pid)

    def _clear_source_errors(self, pid: str) -> None:
        for key in [item for item in self._source_errors if item[0] == pid]:
            self._source_errors.pop(key, None)

    def _turn_result(self, event, turn: Turn) -> tuple[str, str | object | None]:
        if not (event.text or event.raw_text):
            return turn.said, turn.raw_said
        if event.kind is EventKind.ERROR:
            return event.text, None
        return event.text, event.raw_text if event.raw_text is not None else event.text

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
                    idle_streak = idle_streak + 1 if observer.is_idle_screen(capture) else 0
                    if idle_streak >= IDLE_CONFIRMATIONS:
                        if not ended:
                            ended = True
                            self._end_turn_from_screen(pid, capture)
                        self._settle(pid, Status.IDLE)
                    elif idle_streak == 0:
                        ended = False
                        self._settle(pid, Status.WORKING)
                    # A streak of one is undecided.
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
        self._answer_turn(pid, text, raw_result=None)

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
        # be the wrong one. Not every poll: for a file-backed source this is
        # a directory scan.
        if clock.quiet_for(now) > self.relocate:
            batch = await source.refresh()
            # A refused rotation leaves the accepted source untouched. Keep
            # running the other quiet arms and return to the normal poll
            # cadence; this is not an unattached discovery search.
            accepted = self._accept_attachment(pid, source, batch)
            if accepted and self._apply(pid, batch, clock, turns):
                await self._on_progress(pid, observer, batch, clock)
                return
            # The relocate arm has its own throttle, just like screen and
            # rescue. In particular, a rejected candidate remains discoverable
            # on every scan; without this, staging it safely would re-read the
            # foreign transcript on every poll for the rest of its lifetime.
            clock.quiet_since = now

        if clock.screen_quiet_for(now) > self.awaiting:
            await self._check_idle_screen(pid, observer)
            clock.screen_quiet_since = now  # throttle

        if clock.rescue_quiet_for(now) > self.rescue:
            oldest = None
            if self.jobs is not None:
                oldest = self.store.oldest_running_job_for_target(pid)
            # The pane's quiet clock can predate a just-created job; the job
            # itself must have had a full rescue window to finish normally.
            if oldest is None:
                clock.rescue_since = now  # throttle
            elif wall_now() - oldest.created_at > self.rescue:
                await self._rescue_jobs(pid, observer, clock)
                clock.rescue_since = now  # throttle

    async def _screen_only(self, pid: str, observer: HarnessObserver, clock: QuietClock) -> None:
        """The screen arm of `_on_quiet`, for a source that has not attached.

        One arm of the three, not all of them, and that is the whole point of
        keeping it separate rather than calling `_on_quiet` here. A relocate
        asks a source to look somewhere else, which is meaningless before it
        has looked anywhere; and `_rescue_jobs` would finish a caller's job
        with `clock.last_text`, which for a participant nothing has ever been
        read from is the empty string — a silent wrong answer in place of a
        wait. The screen needs neither: it needs a pane, and there is one.

        Same window and same throttle as the arm it mirrors, so a participant
        does not get checked faster for having no transcript.
        """
        await self._screen_status_due(pid, observer, clock)

    async def _screen_status_due(
        self, pid: str, observer: HarnessObserver, clock: QuietClock
    ) -> None:
        """Run the independently throttled status-only screen arm when due.

        Used both before attachment and while an attached source consumes raw
        records that produce no semantic evidence. It cannot relocate a source
        or rescue a job; those decisions retain their own clocks and evidence.
        """
        now = time.monotonic()
        if clock.screen_quiet_since is None:
            clock.screen_quiet_since = now
        if clock.screen_quiet_for(now) > self.awaiting:
            await self._check_idle_screen(pid, observer)
            clock.screen_quiet_since = now  # throttle

    def _accept_attachment(self, pid: str, source: Source, batch: Batch) -> bool:
        """Accept or reject a staged source attachment in one central place.

        The source has not changed its live cursor yet. Collision refusal can
        therefore discard the candidate without losing the participant's own
        accepted transcript — the guarantee that prevents a sibling's later
        writes from leaking into this watcher. This guarantee sits above the
        replaceable per-harness discovery seam because every source needs it.

        A failure before commit/discard also discards the candidate. Otherwise
        one transient store error would leave `_pending` set and every later
        read would fail before the source could recover.
        """
        attached = batch.attached
        if attached is None:
            return True
        decided = False
        try:
            if attached.correlation == str(
                TranscriptProvenance.HEURISTIC
            ) and self._has_cwd_competitor(pid, attached.collision_domain):
                participant = self.store.get_participant(pid)
                logger.warning(
                    "refusing heuristic transcript %s for %s: another live %s "
                    "participant shares its cwd",
                    attached.location,
                    pid,
                    participant.harness if participant is not None else "unknown",
                )
                source.discard_attachment()
                decided = True
                self._handle_attachment_ambiguity(pid, attached)
                return False
            if not is_trusted_provenance(attached.correlation):
                logger.warning(
                    "quarantining heuristic transcript %s for %s: cwd/time is not "
                    "trusted participant identity",
                    attached.location,
                    pid,
                )
                source.discard_attachment()
                decided = True
                self._handle_attachment_ambiguity(pid, attached)
                return False
            if self._trusted_dead_owner_blocks(pid, attached):
                source.discard_attachment()
                decided = True
                self._handle_attachment_ambiguity(pid, attached)
                return False
            owner = self._bound_transcripts.get(attached.location)
            if owner is not None and owner != pid:
                holder = self.store.get_participant(owner)
                if holder is not None and holder.status is not Status.DEAD:
                    prior = self._binding_correlation.get(
                        attached.location, str(TranscriptProvenance.EXACT)
                    )
                    if is_trusted_provenance(attached.correlation) and not (
                        is_trusted_provenance(prior)
                    ):
                        self._revoke_binding(attached.location, owner)
                    else:
                        logger.warning(
                            "transcript %s is already bound to %s (%s); refusing "
                            "to bind it to %s (%s)",
                            attached.location,
                            owner,
                            prior,
                            pid,
                            attached.correlation,
                        )
                        source.discard_attachment()
                        decided = True
                        self._handle_attachment_ambiguity(pid, attached)
                        return False
            source.commit_attachment()
            decided = True
        except Exception:
            if not decided:
                # Preserve the original failure if cleanup itself is broken.
                with contextlib.suppress(Exception):
                    source.discard_attachment()
            raise
        self._on_attach(pid, attached)
        self._clear_source_errors(pid)
        return True

    def _handle_attachment_ambiguity(self, pid: str, attached: Attachment) -> None:
        # A refused rotation leaves an already accepted source intact; that is
        # a safe, still-working observation channel, not a reason to crash its
        # job. Only an initially unbound participant is unable to make progress.
        if pid in self._bound_transcripts.values():
            return
        self._handle_source_error(
            pid,
            Batch(
                error_code=CORRELATION_AMBIGUOUS_CODE,
                error=(
                    f"transcript candidate {attached.location!r} is not uniquely attributable "
                    "to this participant"
                ),
            ),
        )

    def _revoke_binding(self, location: str, owner: str) -> None:
        """Let exact process evidence displace an earlier cwd guess."""
        source = self._sources.get(owner)
        if source is None:
            raise SourceContractError(
                f"cannot revoke heuristic binding {location!r}: owner source is unavailable"
            )
        source.revoke_attachment()
        self._reset_watch_state.add(owner)
        participant = self.store.get_participant(owner)
        bound_session = self._binding_sessions.get(location)
        if participant is not None:
            if participant.session_id == bound_session:
                participant.session_id = None
                participant.session_correlation = None
            # The location itself was revoked regardless of whether a later
            # rotation changed the participant's recorded session id.
            participant.transcript_location = None
            self.store.upsert_participant(participant)
        self._bound_transcripts.pop(location, None)
        self._binding_correlation.pop(location, None)
        self._binding_sessions.pop(location, None)
        self.store.bus_append(
            "agent.observation_error",
            to_id=owner,
            payload={
                "code": "transcript_binding_revoked",
                "message": "an exact process claim displaced this heuristic transcript binding",
            },
        )
        logger.warning(
            "exact transcript claim revoked heuristic binding %s from %s", location, owner
        )

    def _has_cwd_competitor(self, pid: str, collision_domain: str | None) -> bool:
        participant = self.store.get_participant(pid)
        if participant is None or not participant.cwd:
            return False
        cwd = Path(participant.cwd).resolve()
        for other in self.registry.list():
            if (
                other.id == pid
                or other.status is Status.DEAD
                or other.harness != participant.harness
                or not other.cwd
                or Path(other.cwd).resolve() != cwd
            ):
                continue
            other_domain = other.transcript_domain
            if other_domain is None:
                other_source = self._sources.get(other.id)
                other_domain = other_source.collision_domain if other_source is not None else None
            # Distinct declared roots cannot contain the same transcript. An
            # unavailable/undeclared domain stays conservative and competes.
            if (
                collision_domain is not None
                and other_domain is not None
                and collision_domain != other_domain
            ):
                continue
            return True
        return False

    def _trusted_dead_owner_blocks(self, pid: str, attached: Attachment) -> bool:
        """Dead trusted owners keep their transcript unless this is their successor."""
        for other in self.registry.list(include_dead=True):
            if (
                other.id == pid
                or other.status is not Status.DEAD
                or other.transcript_location != attached.location
                or not is_trusted_provenance(other.session_correlation)
            ):
                continue
            same_session = (
                attached.session_id is not None
                and other.session_id is not None
                and attached.session_id == other.session_id
            )
            if same_session and is_trusted_provenance(attached.correlation):
                return False
            logger.warning(
                "transcript %s belongs to dead participant %s (%s); refusing %s (%s)",
                attached.location,
                other.id,
                other.session_correlation,
                pid,
                attached.correlation,
            )
            return True
        return False

    def history_is_ambiguous(self, pid: str, history: History) -> bool:
        """Whether a short-lived history read is only a contested cwd guess."""
        return history_correlation_is_ambiguous(self.registry, pid, history)

    def _on_attach(self, pid: str, attached: Attachment) -> None:
        """Record the effects of an attachment already accepted and committed."""
        # Release any previous binding this participant held, so a rotation
        # (vibe opens a new session directory every turn) frees the old path
        # before claiming the new one.
        self._release_transcript(pid)
        self._bound_transcripts[attached.location] = pid
        self._binding_correlation[attached.location] = attached.correlation
        self._binding_sessions[attached.location] = attached.session_id
        p = self.store.get_participant(pid)
        session_id = attached.session_id
        if p is not None:
            changed = False
            if p.transcript_location != attached.location:
                p.transcript_location = attached.location
                changed = True
            prior = normalize_provenance(p.session_correlation)
            incoming = normalize_provenance(attached.correlation)
            can_update_identity = is_trusted_provenance(incoming) and (
                not is_trusted_provenance(prior) or provenance_at_least(incoming, prior)
            )
            if session_id and p.session_id != session_id and can_update_identity:
                p.session_id = session_id
                p.session_correlation = attached.correlation
                changed = True
            elif (
                session_id and p.session_correlation != attached.correlation and can_update_identity
            ):
                p.session_correlation = attached.correlation
                changed = True
            if changed:
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
        # agent that finished its turn before we attached does not stay IDLE.
        # No history replayed — only the status moves.
        event = attached.last_event
        if event is not None:
            self._settle(pid, status_after(event))
            if event.turn_end:
                result_text, raw_result = self._turn_result(event, Turn(""))
                self._answer_turn(pid, result_text, raw_result=raw_result)

    def _release_transcript(self, pid: str) -> None:
        """Drop a participant's claim on its transcript, if it still holds it.

        Called when a watcher ends, so the path is free for a participant that
        starts later in the same cwd. Only releases the binding this participant
        owns — a collision refusal leaves no binding to release.
        """
        to_drop = [path for path, owner in self._bound_transcripts.items() if owner == pid]
        for path in to_drop:
            del self._bound_transcripts[path]
            self._binding_correlation.pop(path, None)
            self._binding_sessions.pop(path, None)

    def _settle(self, pid: str, desired: Status) -> None:
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD:
            return
        if p.status is desired:
            # Same status, new evidence: last_activity moves, but a status
            # event every quarter second would drown the bus.
            self.registry.touch(pid)
        else:
            self.registry.set_status(pid, desired)

    async def _check_idle_screen(self, pid: str, observer: HarnessObserver) -> None:
        """Map the rendered screen to a status, for any non-DEAD participant.

        The transcript can only settle IDLE or WORKING — see `status_after`.
        A turn boundary that ends in an approval dialog is never read from the
        transcript alone: the agent finishes its turn, the screen shows a modal,
        and the transcript goes quiet, so the participant settles IDLE and the
        gate that would catch the modal is closed before it is ever checked.
        Running for every non-DEAD status closes that gap.

        The mapping is applied regardless of confidence. Being wrong here costs
        a mislabel in the display; the send gate, which is built on the same
        `screen_reading`, requires `high` confidence because being wrong there
        makes a pane permanently unreachable. The asymmetry is deliberate and
        exists because the two consumers pay different costs for the same error.
        """
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD:
            return
        if not p.tmux_pane:
            return
        capture = await self._capture(p.tmux_pane)
        if capture is None:
            return
        reading = observer.screen_reading(capture)
        # PROMPT -> IDLE cannot defer to rescue: `_rescue_jobs` does not touch
        # status, so a participant whose turn ended unobserved would read
        # WORKING forever.
        if reading.kind in (ScreenKind.APPROVAL, ScreenKind.TRUST):
            self._settle(pid, Status.AWAITING_INPUT)
            logger.info("participant %s awaiting input (%s on screen)", pid, reading.kind)
        elif reading.kind is ScreenKind.WORKING:
            self._settle(pid, Status.WORKING)
        elif reading.kind is ScreenKind.PROMPT:
            self._settle(pid, Status.IDLE)
        # UNKNOWN: the screen said nothing the reducer can act on.

    async def _rescue_jobs(self, pid: str, observer: HarnessObserver, clock: QuietClock) -> None:
        """Finish a job whose turn end was never read, so the caller unblocks.

        A missed turn-end record is not hypothetical: a harness can abort a
        turn, rotate its transcript at the wrong moment, or write a boundary
        this parser does not recognise. Whatever the cause, the caller's
        `await_sessions` waits on a promise nothing will ever resolve, and the
        symptom the user sees is a conversation that dies on the second reply.

        So: a long silence, over a screen that shows a bare prompt, with jobs
        still running, is taken as a turn that ended unobserved. The caller
        gets the last thing the agent said and `RESCUE_CODE`, which says
        plainly that this is salvage rather than a declared reply.

        Deliberately narrow. An unreadable screen decides nothing — the same
        rule `_watch_screen` follows — and a participant with no pane cannot
        be rescued at all. Only `ScreenKind.PROMPT` triggers rescue:
        `APPROVAL`/`TRUST` mean the agent is blocked on a modal, not that a
        turn ended, and rescuing there would finish a job the human could
        still complete by acting on the dialog. Status is left alone:
        `_check_idle_screen` has already had its say at a much shorter
        timeout, and this is about the promise, not the participant.
        """
        if self.jobs is None or not self.store.running_jobs_for_target(pid):
            return
        p = self.store.get_participant(pid)
        if p is None or not p.tmux_pane:
            return
        capture = await self._capture(p.tmux_pane)
        if capture is None:
            return
        # Only a bare PROMPT justifies rescue. APPROVAL/TRUST mean the agent
        # is blocked on a modal the human has not dismissed, not that a turn
        # ended.
        if observer.screen_reading(capture).kind is not ScreenKind.PROMPT:
            return
        logger.warning(
            "no turn end seen for %s after %.0fs of quiet; finishing its jobs",
            pid,
            self.rescue,
        )
        self._release_jobs(
            pid,
            clock.last_text,
            error_code=RESCUE_CODE,
            raw_result=None,
        )

    def _answer_turn(
        self,
        pid: str,
        result_text: str,
        heard: Sequence[str] = (),
        *,
        raw_result: str | object | None = _RAW_RESULT_UNSET,
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
        because the spawn prompt *is* the first turn. A promptless spawn does
        the opposite — `methods.py` finishes it immediately as DONE, so it
        never soaks up a turn end and never counts as work in flight that
        would block a `send`.
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
                "%s saw %d turns at %s answer someone else; its prompt never reached the queue",
                job.handle,
                missed,
                pid,
            )
            self._finish(
                job.handle,
                "",
                error_code=UNDELIVERED_CODE,
                state=JobState.CRASHED,
                raw_result=None,
            )
            return
        self._unmatched.pop(job.handle, None)
        self._finish(job.handle, result_text, raw_result=raw_result)

    def _release_jobs(
        self,
        pid: str,
        result_text: str,
        *,
        error_code: str | None = None,
        raw_result: str | object | None = _RAW_RESULT_UNSET,
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
            self._finish(
                job.handle,
                result_text,
                error_code=error_code,
                raw_result=raw_result,
            )

    def _finish(
        self,
        handle: str,
        result_text: str,
        *,
        error_code: str | None = None,
        state: JobState = JobState.DONE,
        raw_result: str | object | None = _RAW_RESULT_UNSET,
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
        if raw_result is _RAW_RESULT_UNSET:
            self.jobs.finish(
                handle,
                state=state,
                result=result_text or "",
                error_code=error_code,
            )
        else:
            self.jobs.finish(
                handle,
                state=state,
                result=result_text or "",
                error_code=error_code,
                raw_result=raw_result,
            )
