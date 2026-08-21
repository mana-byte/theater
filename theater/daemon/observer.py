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

from theater import timing
from theater.config import ObserverSection
from theater.daemon import lineage
from theater.daemon.registry import Registry
from theater.harness import (
    HARNESSES,
    Event,
    EventKind,
    Harness,
    HarnessObserver,
    ScreenConfidence,
    ScreenKind,
    clip,
    status_after,
)
from theater.harness import (
    normalize as normalize_harness,
)
from theater.harness.source import (
    Attachment,
    Batch,
    History,
    IdentityLossEvidence,
    Source,
    SourceContractError,
)
from theater.harness.transcript.observer import open_participant_source
from theater.models import JobState, Status, Tier
from theater.models import now as wall_now
from theater.pricing import usage_cost_microcents
from theater.provenance import (
    TranscriptProvenance,
    is_trusted_provenance,
    normalize_provenance,
    provenance_at_least,
)
from theater.resume_floor import (
    decode_floor,
    floor_authorises_completion,
    floor_is_present,
)
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    canonical_location,
    same_location,
    transcript_identity_recovery_message,
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

#: Log format for a watcher retired by a source contract failure. Used from
#: both the registration and the in-loop handler so the message is in one place.
_SOURCE_CONTRACT_FAILED = "source contract failed for %s; retiring watcher"


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

#: Consecutive relocate/evidence windows that must agree before identity-loss
#: quarantine is entered. One window alone is not enough: a heuristic candidate
#: that appears once and then vanishes is a transient scan artifact, not proof
#: that the trusted pin went stale. The evidence location must repeat across
#: two consecutive relocate windows while the screen reads HIGH/WORKING, and any
#: semantic progress on the pinned source resets this confirmation state.
IDENTITY_LOSS_CONFIRMATIONS = 2

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
            or normalize_harness(other.harness) != normalize_harness(participant.harness)
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
            if history.location is not None and not same_location(
                other.transcript_location, history.location
            ):
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
        self._receipt_candidates: dict[str, tuple[str, str]] = {}
        self._reset_watch_state: set[str] = set()
        #: Live quarantine state. Audit replay populates it once per watcher
        #: lifecycle; predicates consult only this cache and never scan the bus.
        self._identity_lost: set[str] = set()
        self._identity_loss_replayed: set[str] = set()
        #: Pending identity-loss confirmations: pid -> (location, count).
        #: Quarantine is entered only after IDENTITY_LOSS_CONFIRMATIONS
        #: consecutive relocate windows report the SAME evidence location
        #: while the screen reads HIGH/WORKING. Any semantic progress on the
        #: pinned source resets the count to zero.
        self._identity_loss_pending: dict[str, tuple[str, int]] = {}
        self._supervisor: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if not self.harnesses:
            logger.debug("no harnesses configured; observation disabled")
            return
        # Seed watcher-local quarantine caches before the socket can service a
        # send against a participant whose restart audit has not been replayed.
        self._reconcile()
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

    async def reset_for_operator_bind(self, pid: str) -> None:
        """Stop a live watcher so it reopens from the operator-pinned location."""
        task = self._tasks.pop(pid, None)
        self._retired.discard(pid)
        self._reset_watch_state.discard(pid)
        self._clear_source_errors(pid, include_identity_lost=True)
        self._identity_loss_replayed.discard(pid)
        if task is not None:
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task

    def record_operator_binding(
        self,
        pid: str,
        location: str,
        session_id: str | None,
        *,
        prior_owner: str | None = None,
    ) -> None:
        """Mirror an accepted operator binding in the live collision table."""
        loc = canonical_location(location)
        if prior_owner is not None:
            self._release_transcript(prior_owner)
            self._clear_source_errors(prior_owner, include_identity_lost=True)
            self._identity_loss_replayed.discard(prior_owner)
        self._release_transcript(pid)
        self._bound_transcripts[loc] = pid
        self._binding_correlation[loc] = str(TranscriptProvenance.OPERATOR)
        self._binding_sessions[loc] = session_id

    def transcript_identity_lost(self, pid: str) -> bool:
        """Pure cached predicate; only the watch path may enter quarantine."""
        participant = self.store.get_participant(pid)
        if participant is None or participant.status is Status.DEAD:
            return False
        return pid in self._identity_lost

    def _restore_transcript_identity_loss(self, pid: str) -> None:
        """Replay retained audit once for this watcher lifecycle."""
        if pid in self._identity_loss_replayed:
            return
        self._identity_loss_replayed.add(pid)
        participant = self.store.get_participant(pid)
        if participant is None or participant.status is Status.DEAD:
            return
        if not self.store.observation_error_active(pid, TRANSCRIPT_IDENTITY_LOST_CODE):
            return
        self._identity_lost.add(pid)
        # Use the persisted bus timestamp so a daemon restart does not reset
        # ``failed_at`` to ``now()`` and grant endless fresh grace. The bus
        # records the wall-clock ``ts`` of the observation error; if it is
        # unavailable for any reason, fall back to ``now()``.
        persisted_ts = self.store.observation_error_timestamp(pid, TRANSCRIPT_IDENTITY_LOST_CODE)
        failed_at = persisted_ts if persisted_ts is not None else wall_now()
        self._source_errors[(pid, TRANSCRIPT_IDENTITY_LOST_CODE)] = failed_at
        # Restart replay: quarantine begins immediately, but job destruction
        # follows the same OBSERVATION_FAILURE_GRACE as other source errors.
        # A job created just before the daemon died must not be instantly
        # crashed merely because the restart replayed the audit.
        self._sweep_identity_lost_grace(pid, failed_at)

    def _sweep_identity_lost_grace(self, pid: str, failed_at: float | None = None) -> None:
        """Re-evaluate running jobs against the identity-loss grace window.

        Once a participant is quarantined, ``_watch`` takes the screen-only
        branch forever and the normal source-error path that would crash jobs
        after grace never runs again. This sweep closes that gap: it is called
        on every quarantine tick and on restart replay, using the original
        in-memory ``failed_at`` (or the persisted bus timestamp on restart)
        so fresh jobs remain running initially but deterministically crash
        after ``OBSERVATION_FAILURE_GRACE``.
        """
        if self.jobs is None:
            return
        key = (pid, TRANSCRIPT_IDENTITY_LOST_CODE)
        if failed_at is None:
            failed_at = self._source_errors.get(key)
        if failed_at is None:
            return
        now = wall_now()
        for job in self.store.running_jobs_for_target(pid):
            if now - max(failed_at, job.created_at) >= OBSERVATION_FAILURE_GRACE:
                self._finish(
                    job.handle,
                    transcript_identity_recovery_message(pid),
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                    state=JobState.CRASHED,
                    raw_result=None,
                )

    def _evidence_is_bound_to_another_live_participant(
        self, pid: str, evidence: IdentityLossEvidence
    ) -> bool:
        """Whether loss evidence names a transcript another live participant owns.

        The probe found a heuristic candidate that looks newer than the trusted
        pin. Before quarantining on it, the reducer must reject evidence whose
        location or session_id is already claimed by a different live
        participant — otherwise a sibling's legitimate transcript is mistaken
        for identity-loss evidence, and the participant is quarantined for a
        rotation that never happened.

        Three checks, any of which rejects:

        * The evidence location is in ``_bound_transcripts`` under a different
          live pid.
        * The evidence location is persisted as another live participant's
          ``transcript_location``.
        * The evidence ``session_id`` (when the source supplied one) is in
          ``_binding_sessions`` under a different location, or matches another
          live participant's ``session_id``.

        The registry-ownership policy lives here, not in the adapter: the
        source reports the facts (location, session_id) and the reducer decides
        whether they disqualify the evidence. A ``None`` session_id means the
        source could not read one, and only the location checks apply.
        """
        if self._location_bound_to_another_live(pid, evidence.location):
            return True
        return self._session_id_bound_to_another_live(pid, evidence.session_id)

    def _location_bound_to_another_live(self, pid: str, location: str) -> bool:
        """Whether *location* is claimed by a different live participant."""
        owner = self._bound_transcripts.get(canonical_location(location))
        if owner is not None and owner != pid:
            holder = self.store.get_participant(owner)
            if holder is not None and holder.status is not Status.DEAD:
                return True
        for other in self.registry.list():
            if other.id == pid or other.status is Status.DEAD:
                continue
            if same_location(other.transcript_location, location):
                return True
        return False

    def _session_id_bound_to_another_live(self, pid: str, session_id: str | None) -> bool:
        """Whether *session_id* is claimed by a different live participant."""
        if session_id is None:
            return False
        for other in self.registry.list():
            if other.id == pid or other.status is Status.DEAD:
                continue
            if session_id == other.session_id and other.session_id is not None:
                return True
        for loc, sid in self._binding_sessions.items():
            if sid != session_id:
                continue
            bound_pid = self._bound_transcripts.get(loc)
            if bound_pid is not None and bound_pid != pid:
                holder = self.store.get_participant(bound_pid)
                if holder is not None and holder.status is not Status.DEAD:
                    return True
        return False

    def _confirm_identity_loss(self, pid: str, evidence: IdentityLossEvidence) -> bool:
        """Require consecutive relocate windows with the same evidence location.

        One window is not enough: a heuristic candidate that appears once and
        then vanishes — or whose location changes between scans — is a transient
        artifact, not proof that the trusted pin went stale. The evidence
        location must repeat across ``IDENTITY_LOSS_CONFIRMATIONS`` consecutive
        relocate windows while the screen reads HIGH/WORKING. Any semantic
        progress on the pinned source resets the confirmation state, because
        output from the trusted pin means the pin is still alive.

        Returns ``True`` when the confirmation threshold is reached, signalling
        the caller to enter quarantine. Returns ``False`` to keep accumulating.
        """
        pending = self._identity_loss_pending.get(pid)
        canonical = canonical_location(evidence.location)
        count = (
            pending[1] + 1
            if pending is not None and same_location(pending[0], evidence.location)
            else 1
        )
        self._identity_loss_pending[pid] = (canonical, count)
        return count >= IDENTITY_LOSS_CONFIRMATIONS

    def _reset_identity_loss_confirmation(self, pid: str) -> None:
        """Semantic progress on the pinned source resets confirmation."""
        self._identity_loss_pending.pop(pid, None)

    def mark_transcript_identity_lost(self, pid: str, reason: str) -> None:
        """Enter quarantine from positive evidence in the observation path."""
        participant = self.store.get_participant(pid)
        if participant is None or participant.status is Status.DEAD:
            return
        self._handle_source_error(
            pid,
            Batch(
                waiting=True,
                error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                error=transcript_identity_recovery_message(pid, reason),
            ),
        )

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
            harness = self.harnesses.get(normalize_harness(p.harness))
            if harness is None:
                self._warn_unobservable(pid, p)
                continue
            observer = harness.observer
            # A transcript is found by cwd; a screen by pane. So cwd is
            # required for one loop and irrelevant to the other.
            if observer.has_transcript and not p.cwd:
                self._warn_unobservable(pid, p)
                continue
            if p.tier is Tier.SPAWNED and p.tmux_pane is None:
                continue
            self._unobservable.discard(pid)
            watch = self._watch if observer.has_transcript else self._watch_screen
            if observer.has_transcript:
                self._restore_transcript_identity_loss(pid)
            timing.ready_lag("observer.watch", pid, p.created_at, harness=p.harness)
            self._tasks[pid] = asyncio.create_task(watch(pid, normalize_harness(p.harness)))

    def _warn_unobservable(self, pid: str, p) -> None:
        if pid in self._unobservable:
            return
        self._unobservable.add(pid)
        if normalize_harness(p.harness) not in self.harnesses:
            known = ", ".join(sorted(self.harnesses)) or "none"
            reason = f"harness {p.harness!r} is not one we can read (known: {known})"
        else:
            reason = "it reported no working directory"
        logger.warning("cannot observe %s: %s", pid, reason)

    # ---- one participant -----------------------------------------------

    async def _watch(self, pid: str, harness_name: str) -> None:  # noqa: PLR0912, PLR0915
        # Resolved by name on every start so a restarted watcher picks up a
        # registry that has since been reinstalled.
        observer = self.harnesses[harness_name].observer
        source = self._open_source(pid, observer)
        if source is None:
            return
        self._restore_transcript_identity_loss(pid)
        clock = QuietClock()
        turns = TurnAccumulator()

        try:
            # _register_source calls _stage_pending_receipt, which can raise
            # SourceContractError from the ReceiptAdmission validation added
            # in D2.  It must be inside the try so the finally closes the
            # source and removes it from _sources rather than leaking both.
            # A SourceContractError here retires the watcher by the same path
            # as the in-loop handler below, producing the deliberate log line
            # rather than an unretrieved task exception.
            try:
                self._register_source(pid, source)
            except SourceContractError:
                logger.exception(_SOURCE_CONTRACT_FAILED, pid)
                return
            while not self._stopping.is_set():
                try:
                    if pid in self._reset_watch_state:
                        self._reset_watch_state.discard(pid)
                        clock = QuietClock()
                        turns = TurnAccumulator()
                    if self.transcript_identity_lost(pid):
                        # Sweep before screen so a broken third-party screen
                        # classifier cannot starve job terminalization: the
                        # screen arm may raise or hang on capture, but the
                        # grace sweep is pure bookkeeping and must run first.
                        self._sweep_identity_lost_grace(pid)
                        await self._screen_only(pid, observer, clock)
                        await self._sleep(self.search)
                        continue
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
                    logger.exception(_SOURCE_CONTRACT_FAILED, pid)
                    return
                except Exception:
                    logger.exception("observing %s failed", pid)
                await self._sleep(self.poll)
        finally:
            self._clear_source_errors(pid, include_identity_lost=True)
            self._identity_loss_replayed.discard(pid)
            self._reset_watch_state.discard(pid)
            self._receipt_candidates.pop(pid, None)
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
            transcript_domain=p.transcript_domain,
            pane_pid=p.live_pid,
        )
        if source.collision_domain is not None and p.transcript_domain != source.collision_domain:
            p.transcript_domain = source.collision_domain
            self.store.upsert_participant(p)
        return source

    def _register_source(self, pid: str, source: Source) -> None:
        self._sources[pid] = source
        self._stage_pending_receipt(pid, source)

    @staticmethod
    def _validate_batch(source: Source, batch: Batch) -> None:
        """Reject contradictory source facts without stranding a candidate."""
        if not (batch.waiting and batch.attached is not None):
            return
        source.discard_attachment()
        raise SourceContractError(
            f"{type(source).__name__} returned a batch that is both waiting and attached"
        )

    def _record_usage(self, pid: str, event: Event) -> bool:
        """Persist a usage report, returning whether it was new."""
        assert event.usage is not None
        u = event.usage
        participant = self.store.get_participant(pid)
        usage_key = u.idempotency_key
        if usage_key is not None and participant is not None:
            scope = participant.session_id or participant.transcript_location
            if scope:
                usage_key = f"{scope}:{usage_key}"
        return self.store.record_usage(
            participant_id=pid,
            tree_root_id=lineage.root_of(self.store, pid),
            usage_key=usage_key,
            ts=event.ts if event.ts is not None else wall_now(),
            model=u.model,
            harness=participant.harness if participant is not None else "unknown",
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_creation_input_tokens=u.cache_creation_input_tokens,
            cache_read_input_tokens=u.cache_read_input_tokens,
            reasoning_output_tokens=u.reasoning_output_tokens,
            cost_microcents=usage_cost_microcents(u),
        )

    def _apply(self, pid: str, batch: Batch, clock: QuietClock, turns: TurnAccumulator) -> bool:  # noqa: PLR0912
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
            if event.usage is not None:
                self._record_usage(pid, event)
            if event.usage_only:
                continue
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

        # A non-attachment batch with actual source growth (events or progressed
        # bytes from the committed cursor) is necessarily post-attach/post-launch.
        # If a resume floor is still present from a suppressed earlier attachment,
        # this growth proves the successor has moved beyond the floor — clear it
        # via targeted update so the participant's own status/last_activity are
        # not reverted. Empty or status-only polls do not clear.
        if batch.attached is None and (batch.progressed or batch.events):
            p_now = self.store.get_participant(pid)
            if p_now is not None and floor_is_present(p_now.resume_floor):
                self.store.clear_resume_floor(pid)

        return batch.progressed or bool(batch.events) or batch.attached is not None

    @staticmethod
    def _has_semantic_progress(batch: Batch) -> bool:
        """Whether a batch says something about the participant, not just its source.

        ``progressed`` includes bookkeeping and unknown records. Conversation
        events, status and attachments can also restart the screen-status clock.
        """
        return (
            any(not event.usage_only for event in batch.events)
            or batch.status is not None
            or batch.attached is not None
        )

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
        identity_was_active = pid in self._identity_lost
        if batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
            participant = self.store.get_participant(pid)
            if participant is None or participant.status is Status.DEAD:
                return
            self._identity_lost.add(pid)
        for stale in [item for item in self._source_errors if item[0] == pid and item != key]:
            self._source_errors.pop(stale, None)
        failed_at = self._source_errors.get(key)
        if failed_at is None:
            failed_at = wall_now()
            self._source_errors[key] = failed_at
            logger.error("observation failed for %s: %s", pid, batch.error or batch.error_code)
            if not (batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE and identity_was_active):
                self.store.bus_append(
                    "agent.observation_error",
                    to_id=pid,
                    payload={"code": batch.error_code, "message": batch.error or ""},
                )
        if self.jobs is None:
            return
        if batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
            # Quarantine/watch behaviour may begin immediately — the participant
            # is already in ``_identity_lost`` — but job destruction must follow
            # the same ``max(first_failure, job.created_at)`` grace as other
            # source errors. A source that just started failing must not
            # instantly crash a prompt created moments ago against a still-live
            # pane, and restart replay (``_restore_transcript_identity_loss``)
            # must not crash jobs that are still within their grace window.
            self._sweep_identity_lost_grace(pid, failed_at)
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
            # Reset the identity-loss confirmation counter only on actual source
            # progress (semantic events, raw bookkeeping, or a fresh attachment),
            # not merely on a clean Batch() from a normal empty poll. An empty
            # poll has error_code None but no progress, and resetting on it
            # would clear confirmation between every pair of 5s relocate windows,
            # making the threshold unreachable. This does not touch the three
            # quiet timers.
            #
            # Why this aligns with ``_apply``'s edge-progress rather than
            # ``Batch.status`` levels: ``_apply`` returns True when
            # ``batch.progressed or bool(batch.events) or batch.attached is not
            # None`` — the same predicate used here. A ``Batch.status`` set
            # without any of those is a source-derived classification with no
            # new content from the pinned transcript, so it does not prove the
            # pin is alive. The reset must track the same evidence that
            # ``_apply`` treats as "the source produced something", because
            # that is exactly the signal that the trusted pin is still writing
            # — which is what makes identity-loss confirmation stale. (Per
            # Ferramondo: confirmation reset is an edge event, not a level.)
            if batch.progressed or bool(batch.events) or batch.attached is not None:
                self._reset_identity_loss_confirmation(pid)

    def _clear_source_errors(self, pid: str, *, include_identity_lost: bool = False) -> None:
        for key in [item for item in self._source_errors if item[0] == pid]:
            if key[1] == TRANSCRIPT_IDENTITY_LOST_CODE and not include_identity_lost:
                continue
            self._source_errors.pop(key, None)
        if include_identity_lost:
            self._identity_lost.discard(pid)
            self._identity_loss_pending.pop(pid, None)

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
            self._validate_batch(source, batch)
            self._report_source_error(pid, batch)
            untrusted_refresh = batch.attached is not None and self._is_untrusted_rotation(
                pid, batch.attached
            )
            if untrusted_refresh:
                # Defence for third-party sources: heuristic movement is not
                # admissible through refresh and is not loss evidence either.
                # Only the bounded, non-committable probe below can supply it.
                source.discard_attachment()
            # A refused rotation leaves the accepted source untouched. Keep
            # running the other quiet arms and return to the normal poll
            # cadence; this is not an unattached discovery search.
            accepted = not untrusted_refresh and self._accept_attachment(pid, source, batch)
            if accepted and self._apply(pid, batch, clock, turns):
                await self._on_progress(pid, observer, batch, clock)
                return
            evidence = await source.probe_identity_loss()
            if (
                evidence is not None
                and not self._evidence_is_bound_to_another_live_participant(pid, evidence)
                and await self._screen_is_positively_working(pid, observer)
            ):
                if self._confirm_identity_loss(pid, evidence):
                    self.mark_transcript_identity_lost(
                        pid,
                        (
                            "a newer same-harness/cwd transcript candidate appeared while the "
                            "trusted pin was inert and the pane was visibly working: "
                            f"{evidence.location}"
                        ),
                    )
                clock.quiet_since = now
                return
            # A relocate window that found no admissible evidence — whether the
            # probe returned None, the evidence was bound to another live
            # participant, or the screen was not HIGH/WORKING — breaks the
            # consecutive chain. Reset so the next qualifying window starts
            # fresh rather than accumulating against a stale predecessor.
            self._reset_identity_loss_confirmation(pid)
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

    def _is_untrusted_rotation(self, pid: str, attached: Attachment) -> bool:
        participant = self.store.get_participant(pid)
        return (
            participant is not None
            and participant.transcript_location is not None
            and not same_location(participant.transcript_location, attached.location)
            and is_trusted_provenance(participant.session_correlation)
            and not is_trusted_provenance(attached.correlation)
        )

    async def _screen_is_positively_working(self, pid: str, observer: HarnessObserver) -> bool:
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD or not p.tmux_pane:
            return False
        capture = await self._capture(p.tmux_pane)
        if capture is None:
            return False
        reading = observer.screen_reading(capture)
        self._apply_screen_reading(pid, reading)
        return reading.kind is ScreenKind.WORKING and reading.confidence is ScreenConfidence.HIGH

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
            owner = self._bound_transcripts.get(canonical_location(attached.location))
            if owner is not None and owner != pid:
                holder = self.store.get_participant(owner)
                if holder is not None and holder.status is not Status.DEAD:
                    bound_loc = canonical_location(attached.location)
                    prior = self._binding_correlation.get(
                        bound_loc, str(TranscriptProvenance.EXACT)
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
        self._clear_source_errors(pid, include_identity_lost=True)
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
        loc = canonical_location(location)
        participant = self.store.get_participant(owner)
        bound_session = self._binding_sessions.get(loc)
        if participant is not None:
            if participant.session_id == bound_session:
                participant.session_id = None
                participant.session_correlation = None
            # The location itself was revoked regardless of whether a later
            # rotation changed the participant's recorded session id.
            participant.transcript_location = None
            self.store.upsert_participant(participant)
        self._bound_transcripts.pop(loc, None)
        self._binding_correlation.pop(loc, None)
        self._binding_sessions.pop(loc, None)
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
                or normalize_harness(other.harness) != normalize_harness(participant.harness)
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
                or not same_location(other.transcript_location, attached.location)
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

    def transcript_receipt(self, pid: str, *, location: str, session_id: str) -> str:
        """Stage exact receipt evidence without persisting it before admission."""
        source = self._sources.get(pid)
        if source is None:
            self._receipt_candidates[pid] = (location, session_id)
            return "staged"
        return self._stage_receipt_source(pid, source, location=location, session_id=session_id)

    def _stage_pending_receipt(self, pid: str, source: Source) -> None:
        candidate = self._receipt_candidates.pop(pid, None)
        if candidate is None:
            return
        location, session_id = candidate
        self._stage_receipt_source(pid, source, location=location, session_id=session_id)

    def _stage_receipt_source(
        self, pid: str, source: Source, *, location: str, session_id: str
    ) -> str:
        result = source.admit_exact_location(location=location, session_id=session_id)
        if result not in ("accepted", "staged"):
            raise SourceContractError(
                f"{type(source).__name__}.admit_exact_location() must return 'accepted' or "
                f"'staged' (the ReceiptAdmission literal), got {result!r}. A source that "
                "cannot admit a receipt should raise rather than return None or another value, "
                "because a silent non-admission tells the caller the receipt worked while "
                "nothing is persisted."
            )
        if result == "accepted":
            loc = canonical_location(location)
            self.store.record_transcript_receipt(
                pid,
                session_id=session_id,
                transcript_location=loc,
            )
            self._binding_correlation[loc] = str(TranscriptProvenance.EXACT)
            self._binding_sessions[loc] = session_id
        if result in {"accepted", "staged"}:
            # A staged exact receipt deliberately has not persisted ownership
            # yet, but it must re-arm the watcher so the reducer can inspect
            # and commit that exact attachment on its next read.
            self._clear_source_errors(pid, include_identity_lost=True)
        return result

    def _on_attach(self, pid: str, attached: Attachment) -> None:
        """Record the effects of an attachment already accepted and committed."""
        # Release any previous binding this participant held, so a rotation
        # (vibe opens a new session directory every turn) frees the old path
        # before claiming the new one.
        self._release_transcript(pid)
        # Canonicalise once at the source boundary: every downstream dict key
        # and persisted field uses the canonical spelling so two sources that
        # report ``~/t.jsonl`` and ``/Users/me/t.jsonl`` for the same file do
        # not read as different transcripts. Opaque ``scheme://`` locations
        # pass through unchanged.
        loc = canonical_location(attached.location)
        self._bound_transcripts[loc] = pid
        self._binding_correlation[loc] = attached.correlation
        self._binding_sessions[loc] = attached.session_id
        p = self.store.get_participant(pid)
        session_id = attached.session_id
        if p is not None:
            changed = False
            # Converge opportunistically: even when same_location says the
            # stored and incoming paths name the same file, the stored
            # spelling may carry a ``..`` segment or un-expanded symlink that
            # reaches plugins verbatim (observer known_location, methods
            # read_transcript, recall_read, spawner resume). Rewriting to
            # the canonical form here is one cheap write that closes all
            # four sites at once, without a migration.
            if p.transcript_location != loc:
                p.transcript_location = loc
                changed = True
            prior = normalize_provenance(p.session_correlation)
            incoming = normalize_provenance(attached.correlation)
            can_update_identity = is_trusted_provenance(incoming) and (
                not is_trusted_provenance(prior) or provenance_at_least(incoming, prior)
            )
            if session_id and p.session_id != session_id and can_update_identity:
                timing.ready_lag("observer.attach", pid, p.created_at, harness=p.harness)
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
        #
        # A resume floor suppresses both status and completion unless the
        # attachment is provably the same stream (same device/inode, non-shrunk
        # size, strictly beyond the saved record count). The floor is NOT
        # cleared by a suppressed or eventless first attachment: it must survive
        # daemon restart so reattaching to the same predecessor turn_end remains
        # suppressed. It is cleared only when (a) an attach point is authorised
        # as provably beyond the floor, or (b) after an earlier suppressed
        # accepted attachment, a later non-attachment batch carries actual
        # source growth/events from the committed cursor (those bytes are
        # necessarily post-attach/post-launch). A floor of ``None``
        # (cold/adopted) preserves the fast-spawn behaviour exactly.
        floor_raw = p.resume_floor if p is not None else None
        if attached.last_event is not None and not floor_is_present(floor_raw):
            self._settle_from_event(pid, attached.last_event)
        elif attached.last_event is not None and floor_is_present(floor_raw):
            floor = decode_floor(floor_raw)
            if floor_authorises_completion(floor, floor_raw=floor_raw, point=attached.point):
                self._settle_from_event(pid, attached.last_event)
                # Authorised: the attach point proves the successor is past
                # the floor. Clear via targeted update so status/last_activity
                # changes made by _settle are not reverted.
                self.store.clear_resume_floor(pid)
            else:
                logger.info(
                    "resume floor suppresses attach-derived status for %s (floor=%s, point=%s)",
                    pid,
                    floor_raw,
                    attached.point,
                )
        # If the floor was present but the attach was suppressed or eventless,
        # the floor is deliberately left in place. It will be cleared by the
        # first later batch that carries actual source growth from the
        # committed cursor (see _apply).

    def _settle_from_event(self, pid: str, event: Event) -> None:
        """Settle status and answer a turn from an attach-time event."""
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
        self._apply_screen_reading(pid, reading)

    def _apply_screen_reading(self, pid: str, reading) -> None:
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

    def _finish_identity_lost_jobs(self, pid: str, result_text: str) -> None:
        if self.jobs is None:
            return
        for job in self.store.running_jobs_for_target(pid):
            self._finish(
                job.handle,
                result_text,
                error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                state=JobState.CRASHED,
                raw_result=None,
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
