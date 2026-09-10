"""First-class live/durable source composition for native runtime wiring.

``HybridSource`` is deliberately not another ``CompositeSource`` enrichment.
Enrichment channels contribute trajectory facts only and can never drive
authoritative events or status; a live channel declared through
``HarnessManifest.runtime`` is a different authority regime entirely, so it
gets its own composition with explicit rules:

* the durable reader (transcript or database) keeps attachment, identity,
  resume floors, history, and the persisted checkpoint cursor exactly as
  before — a live socket is never labelled a transcript or a database;
* the live channel owns current-turn deltas, the authoritative status while
  it is healthy, and exact native terminal evidence — the only thing that
  may complete a Theater job for a natively wired participant;
* a delayed durable record enriches history but cannot reopen a turn the
  live channel already reported terminal, and cannot regress the status the
  live channel last reported while it stays healthy;
* on live overflow, error, or disconnect the composition degrades visibly
  through channel health and reconciles from durable state; it never
  invents completion and never silently drops terminal evidence.

Checkpoint acknowledgement and rollback are forwarded to both halves
independently: the durable cursor is the persisted one (its format is
already what ``participant.source_checkpoint`` stores), while unacknowledged
live terminal evidence survives a rollback by replay, because unlike a
replaceable text delta it can never be produced again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
from collections import OrderedDict
from collections.abc import Sequence

from theater.constants.core import HARNESS_NAME
from theater.constants.harness import (
    HARNESS_CHANNEL_ID_MAX_CHARS,
    HARNESS_DEDUPE_MAX_FACTS,
    HARNESS_ENRICHMENT_READ_TIMEOUT_SECONDS,
)
from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.harness.channels.health import (
    ChannelHealthTracker,
    merge_channel_health,
    read_error_diagnostic,
    read_exception_diagnostic,
)
from theater.harness.channels.wakeup import WakeupSignal
from theater.harness.contracts.channels import (
    ChannelDeclaration,
    ChannelHealth,
    ChannelHealthState,
    ChannelKind,
)
from theater.harness.contracts.events import Event
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    NativeTurnOutcome,
)
from theater.harness.contracts.source import (
    Batch,
    History,
    HistoryPage,
    IdentityLossEvidence,
    ReceiptAdmission,
    Source,
)
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Status
from theater.trajectory.enums import TrajectoryStatus

_DURABLE_KINDS = frozenset({ChannelKind.TRANSCRIPT, ChannelKind.DATABASE})
_TERMINAL_FACT_STATUSES = frozenset({TrajectoryStatus.COMPLETED, TrajectoryStatus.ERROR})
#: A live source reporting one of these states cannot hold status authority,
#: whatever it last broadcast. STARTING/INACTIVE have no authority to lose
#: (they never reported a status) and stay neutral.
_UNHEALTHY_STATES = frozenset({ChannelHealthState.DEGRADED, ChannelHealthState.FAILED})
_DEFAULT_TIMEOUT = HARNESS_ENRICHMENT_READ_TIMEOUT_SECONDS
_DEDUPE_MAX = HARNESS_DEDUPE_MAX_FACTS
_SAFE_TYPE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

logger = logging.getLogger("theater.harness.channels")


class HybridSourceError(ValueError):
    """Construction-time validation failure for the live/durable wiring."""


def _validate_channel_id(channel_id: object, label: str) -> None:
    if (
        not isinstance(channel_id, str)
        or not channel_id.strip()
        or len(channel_id) > HARNESS_CHANNEL_ID_MAX_CHARS
        or not HARNESS_NAME.fullmatch(channel_id)
    ):
        raise HybridSourceError(f"{label} must be a bounded canonical channel id")


class HybridSource(Source):
    """One durable reader plus the runtime's single live channel.

    The durable source remains the authority for attachment, identity,
    resume floors, and history. The live source is authoritative for
    current-turn content, status while healthy, and exact terminal evidence.
    ``wakeup`` is optional: when provided, live reads that produced something
    wake it so the observer's watch loop re-reads promptly instead of
    waiting out its poll interval (the polling fallback still applies).
    """

    def __init__(
        self,
        *,
        durable: Source,
        live: Source,
        live_channel: LiveChannelDeclaration,
        durable_channel: ChannelDeclaration | None = None,
        durable_channel_id: str = "primary",
        live_read_timeout: float = _DEFAULT_TIMEOUT,
        wakeup: WakeupSignal | None = None,
    ) -> None:
        self._validate_construction(
            durable, live, live_channel, durable_channel, durable_channel_id, live_read_timeout
        )
        self._durable = durable
        self._live = live
        self._live_channel = live_channel
        self._durable_channel = durable_channel
        self._durable_channel_id = durable_channel_id
        self._live_channel_id = live_channel.channel.id
        self._live_read_timeout = live_read_timeout
        self._wakeup = wakeup
        self._durable_health = ChannelHealthTracker(durable_channel_id)
        self._durable_health.mark_starting()
        self._live_health = ChannelHealthTracker(self._live_channel_id)
        self._live_health.mark_starting()
        # ---- live authority window ----------------------------------------
        self._live_status: Status | None = None
        self._live_healthy: bool = True
        # ---- exact-evidence staging ---------------------------------------
        # Terminal evidence that left the live source but has not been
        # acknowledged as durably routed. Unacknowledged evidence is retained
        # across reads — the next read replays it — so a sink failure or a
        # crash before acknowledgement can never silently drop it. The sink's
        # first-write-wins makes every replay harmless, and acknowledgement
        # (which only happens after successful routing) clears it.
        self._held_evidence: tuple[NativeTurnOutcome, ...] = ()
        # ---- irreversible completed items ----------------------------------
        self._terminal_native_ids: OrderedDict[str, None] = OrderedDict()
        # ---- emitted event identity -----------------------------------------
        # Identified events already emitted by a previous read. A later
        # durable replay of an already-emitted native event is suppressed:
        # the durable reader may re-serve records after a late attach, but a
        # native item is heard exactly once. Bounded like the fact ledger;
        # a rollback un-emits the last read's ids so a failed apply can be
        # re-read losslessly.
        self._emitted_event_ids: OrderedDict[str, None] = OrderedDict()
        self._last_read_event_ids: tuple[str, ...] = ()
        self._closed = False

    # ---- construction validation ------------------------------------------

    @staticmethod
    def _validate_construction(
        durable: Source,
        live: Source,
        live_channel: LiveChannelDeclaration,
        durable_channel: ChannelDeclaration | None,
        durable_channel_id: str,
        live_read_timeout: float,
    ) -> None:
        """Validate the effective wiring ownership, at composition time.

        The live channel must be the runtime's first-class LIVE declaration,
        the durable reader must be a transcript or database channel, and the
        two channel ids must differ: a live socket is never a transcript or
        database surrogate, and the durable reader is never live.
        """
        if not isinstance(durable, Source):
            raise HybridSourceError("durable must implement Source")
        if not isinstance(live, Source):
            raise HybridSourceError("live must implement Source")
        if not isinstance(live_channel, LiveChannelDeclaration):
            raise HybridSourceError("live_channel must be a LiveChannelDeclaration")
        if live_channel.channel.kind is not ChannelKind.LIVE:
            raise HybridSourceError("live channel declaration kind must be ChannelKind.LIVE")
        _validate_channel_id(durable_channel_id, "durable_channel_id")
        if durable_channel is not None:
            if not isinstance(durable_channel, ChannelDeclaration):
                raise HybridSourceError("durable_channel must be a ChannelDeclaration or null")
            if durable_channel.kind not in _DURABLE_KINDS:
                raise HybridSourceError(
                    "durable channel kind must be transcript or database, never "
                    f"{durable_channel.kind.value}"
                )
            if durable_channel.id == live_channel.channel.id:
                raise HybridSourceError(
                    "durable and live channel ids must differ: "
                    f"{durable_channel.id!r} is declared twice"
                )
        if durable_channel_id == live_channel.channel.id:
            raise HybridSourceError(
                "durable_channel_id must not duplicate the live channel id "
                f"{live_channel.channel.id!r}"
            )
        if (
            not isinstance(live_read_timeout, (int, float))
            or not math.isfinite(live_read_timeout)
            or live_read_timeout <= 0
        ):
            raise HybridSourceError("live_read_timeout must be a finite number > 0")

    # ---- identity ----------------------------------------------------------

    @property
    def collision_domain(self) -> str | None:  # type: ignore[override]
        return self._durable.collision_domain

    @property
    def live_channel_id(self) -> str:
        return self._live_channel_id

    @property
    def durable_channel_id(self) -> str:
        return self._durable_channel_id

    # ---- reading -----------------------------------------------------------

    async def read(self) -> Batch:
        durable = await self._read_durable()
        live = await self._read_live()
        # Unacknowledged evidence is retained and replayed alongside anything
        # new: a read never overwrites held terminal evidence.
        evidence = (*self._held_evidence, *live.terminal_evidence)
        self._held_evidence = evidence
        if evidence and self._wakeup is not None:
            self._wakeup.wake()
        # Snapshot the completed-native ledger before this read's fact merge:
        # an event for an item completed in a previous read is a late replay
        # and is dropped, while this read's own completion events pass.
        prior_terminal = frozenset(self._terminal_native_ids)
        # Snapshot the emitted-event ledger too: a durable record replayed
        # after its live counterpart was already emitted is suppressed.
        prior_emitted = frozenset(self._emitted_event_ids)
        events = self._merge_events(durable.events, live.events, prior_terminal, prior_emitted)
        self._last_read_event_ids = tuple(
            event.native_id for event in events if event.native_id is not None
        )
        for native_id in self._last_read_event_ids:
            self._note_emitted_event(native_id)
        facts = self._merge_facts(durable.trajectory, live.trajectory)
        status = self._merged_status(durable, live)
        # Retained evidence must not force has_more: until it is acknowledged
        # it would spin the observer with zero-length polls. Its replay rides
        # the ordinary bounded poll cadence.
        has_more = durable.has_more or live.has_more
        # Progressed reflects new work, not a retained-evidence replay: the
        # replay must not keep the observer's quiet timers from running.
        progressed = durable.progressed or live.progressed or bool(live.terminal_evidence)
        return Batch(
            events=events,
            progressed=progressed,
            has_more=has_more,
            status=status,
            attached=durable.attached,
            waiting=durable.waiting and live.waiting,
            error_code=durable.error_code,
            error=durable.error,
            trajectory=facts,
            trajectory_events=durable.trajectory_events,
            terminal_evidence=evidence,
        )

    async def _read_durable(self) -> Batch:
        tracker = self._durable_health
        try:
            batch = await self._durable.read()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            tracker.mark_failed(read_exception_diagnostic("durable read failed", exc))
            raise
        if batch.error_code is not None:
            tracker.mark_degraded(read_error_diagnostic("durable", batch.error_code))
        else:
            tracker.record_success()
            tracker.mark_healthy()
        return batch

    async def _read_live(self) -> Batch:
        """Read the live channel, bounded and fail-open for durable observation.

        A live failure is channel degradation, never a durable observation
        failure: the merged batch keeps flowing from the durable reader and
        terminal evidence already held is preserved for replay.
        """
        tracker = self._live_health
        try:
            batch = await asyncio.wait_for(self._live.read(), timeout=self._live_read_timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._live_healthy = False
            tracker.mark_degraded(
                f"live read timeout after {self._live_read_timeout}s; observing from durable state"
            )
            return Batch()
        except Exception as exc:
            self._live_healthy = False
            tracker.mark_failed(read_exception_diagnostic("live read failed", exc))
            return Batch()
        if not isinstance(batch, Batch):
            self._live_healthy = False
            tracker.mark_failed(f"live returned non-Batch ({_type_name(batch)})")
            return Batch()
        if batch.error_code is not None:
            self._live_healthy = False
            tracker.mark_degraded(read_error_diagnostic("live", batch.error_code))
            return batch
        # The live source's own health snapshot is authoritative about its
        # connection: a normal (even empty) batch from a source reporting
        # DEGRADED/DISCONNECTED/FAILED relinquishes live status authority, so
        # a remembered live status can never mask a disconnect.
        source_health = _source_health(self._live, self._live_channel_id)
        if source_health is not None and source_health.state in _UNHEALTHY_STATES:
            self._live_healthy = False
            tracker.mark_degraded(
                f"live channel reports {source_health.state.value}; observing from durable state"
            )
            return batch
        self._live_healthy = True
        tracker.record_success()
        tracker.mark_healthy()
        return batch

    # ---- status authority ----------------------------------------------------

    def _merged_status(self, durable: Batch, live: Batch) -> Status | None:
        """Live status wins while the live channel is healthy.

        While live is healthy, a delayed durable record cannot regress the
        status the live channel reported — enrichment, not time travel. When
        the live channel has never spoken or is degraded, the durable reader
        infers status exactly as before (durable fallback and reconciliation).
        """
        if live.status is not None and self._live_healthy:
            self._live_status = live.status
            return live.status
        if self._live_healthy and self._live_status is not None:
            return self._live_status
        return durable.status

    # ---- trajectory reconciliation --------------------------------------------

    def _merge_facts(
        self,
        durable_facts: Sequence[TrajectoryFact],
        live_facts: Sequence[TrajectoryFact],
    ) -> tuple[TrajectoryFact, ...]:
        """Reconcile live and durable facts by native identity, not text.

        Facts for one native id are deduplicated whether they arrived over
        the live channel or the durable reader: the highest revision wins,
        ties prefer the terminal status, then the durable fact, because the
        durable parser is canonical for history. Once an item is terminal,
        it stays terminal — a later replaceable delta can never reopen a
        completed native item.
        """
        best: OrderedDict[str, TrajectoryFact] = OrderedDict()
        plain: list[TrajectoryFact] = []
        for fact in (*durable_facts, *live_facts):
            if fact.native_id is None:
                plain.append(fact)
                continue
            current = best.get(fact.native_id)
            if current is None:
                best[fact.native_id] = fact
                continue
            best[fact.native_id] = _newer_fact(current, fact)
        result: list[TrajectoryFact] = []
        for native_id, fact in best.items():
            if fact.status in _TERMINAL_FACT_STATUSES:
                self._note_terminal_fact(native_id)
                result.append(fact)
                continue
            if native_id in self._terminal_native_ids:
                continue
            result.append(fact)
        # Facts without a native id have no identity to reconcile and no
        # completion to protect; they pass through in arrival order.
        result.extend(plain)
        return tuple(result)

    def _note_terminal_fact(self, native_id: str) -> None:
        self._terminal_native_ids[native_id] = None
        while len(self._terminal_native_ids) > _DEDUPE_MAX:
            self._terminal_native_ids.popitem(last=False)

    def _note_emitted_event(self, native_id: str) -> None:
        self._emitted_event_ids.pop(native_id, None)
        self._emitted_event_ids[native_id] = None
        while len(self._emitted_event_ids) > _DEDUPE_MAX:
            self._emitted_event_ids.popitem(last=False)

    def _merge_events(
        self,
        durable_events: Sequence[Event],
        live_events: Sequence[Event],
        prior_terminal: frozenset[str],
        prior_emitted: frozenset[str],
    ) -> tuple[Event, ...]:
        """Reconcile identified events by native identity, never by text.

        Only events carrying a ``native_id`` participate: anonymous legacy
        events pass through untouched in arrival order. Within one read,
        identified events for one native id are deduplicated — the highest
        revision wins, ties keep the earlier (durable) event. An event for a
        native item that a previous read already emitted or saw complete is
        a late replay and is dropped, so a native item is heard exactly once
        however often live or durable history repeats it.
        """
        slots: dict[str, int] = {}
        output: list[Event] = []
        for event in (*durable_events, *live_events):
            if event.native_id is None:
                output.append(event)
                continue
            if event.native_id in prior_terminal or event.native_id in prior_emitted:
                continue
            index = slots.get(event.native_id)
            if index is None:
                slots[event.native_id] = len(output)
                output.append(event)
                continue
            if _newer_event(output[index], event) is event:
                output[index] = event
        return tuple(output)

    # ---- attachment, identity, and history stay durable ------------------------

    async def refresh(self) -> Batch:
        batch = await self._durable.refresh()
        if batch.attached is not None and self._live_healthy and self._live_status is not None:
            # A rotation attach settles from its own last event; while live
            # is healthy its status stays authoritative, so the attach
            # enriches the location without regressing current status.
            return Batch(
                events=batch.events,
                progressed=batch.progressed,
                has_more=batch.has_more,
                status=self._live_status,
                attached=batch.attached,
                waiting=batch.waiting,
                error_code=batch.error_code,
                error=batch.error,
                trajectory=batch.trajectory,
                trajectory_events=batch.trajectory_events,
            )
        return batch

    async def probe_identity_loss(self) -> IdentityLossEvidence | None:
        return await self._durable.probe_identity_loss()

    def commit_attachment(self) -> None:
        self._durable.commit_attachment()

    def discard_attachment(self) -> None:
        self._durable.discard_attachment()

    def revoke_attachment(self) -> None:
        self._durable.revoke_attachment()

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        return self._durable.admit_exact_location(location=location, session_id=session_id)

    async def history(self, *, last_n: int) -> History:
        return await self._durable.history(last_n=last_n)

    async def history_page(
        self,
        *,
        before: str | None = None,
        snapshot: str | None = None,
        limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
        include_full_text: bool = False,
    ) -> HistoryPage:
        return await self._durable.history_page(
            before=before,
            snapshot=snapshot,
            limit=limit,
            include_full_text=include_full_text,
        )

    # ---- checkpoints: durable cursor, live evidence retention ------------------

    def source_checkpoint(self) -> str | None:
        """The persisted checkpoint is the durable reader's cursor."""
        return self._durable.source_checkpoint()

    def pending_source_checkpoint(self) -> str | None:
        return self._durable.pending_source_checkpoint()

    def pending_terminal_evidence(self) -> bool:
        """Whether terminal evidence is held awaiting successful routing.

        The observer consults this before acknowledging the source checkpoint:
        while exact evidence has not been durably routed, the checkpoint
        stays unacknowledged so the evidence cannot be lost to an
        acknowledgement that clears it.
        """
        return bool(self._held_evidence)

    def acknowledge_source_checkpoint(self) -> None:
        """Both halves advance once the batch — evidence included — is durable."""
        self._held_evidence = ()
        self._last_read_event_ids = ()
        self._durable.acknowledge_source_checkpoint()
        with contextlib.suppress(Exception):
            self._live.acknowledge_source_checkpoint()

    def terminal_evidence_delivered(self) -> None:
        """Mark held terminal evidence as durably routed.

        Independent of the durable cursor: the observer calls this when the
        evidence sink has persisted every held outcome, even on paths (an
        attachment rejection, a failed apply) where the durable cursor must
        not advance.
        """
        self._held_evidence = ()
        self._last_read_event_ids = ()

    def rollback_source_checkpoint(self) -> None:
        """Rewind the durable cursor; held live evidence replays by retention.

        Replaceable live deltas are dropped with the rolled-back batch, but
        exact terminal evidence can never be produced again, so unacknowledged
        evidence stays held and the next read replays it. The sink's
        first-write-wins makes the replay idempotent. The last read's emitted
        event identities are un-emitted so a re-read of the same records
        re-emits them instead of suppressing a batch that never applied.
        """
        if self._held_evidence:
            logger.debug(
                "rolling back a batch with live terminal evidence; %d outcome(s) retained",
                len(self._held_evidence),
            )
        for native_id in self._last_read_event_ids:
            self._emitted_event_ids.pop(native_id, None)
        self._last_read_event_ids = ()
        self._durable.rollback_source_checkpoint()
        with contextlib.suppress(Exception):
            self._live.rollback_source_checkpoint()

    def arm_terminal_evidence_replay(self) -> None:
        """Retain held evidence for replay after a processing failure.

        Retention-until-acknowledgement makes this implicit: unacknowledged
        evidence is never overwritten by a later read. The hook stays so
        callers that already name the failure keep their intent.
        """
        if self._held_evidence:
            logger.debug(
                "terminal evidence retained for replay; %d outcome(s) held",
                len(self._held_evidence),
            )

    # ---- health -------------------------------------------------------------------

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        durable = self._durable_health.snapshot()
        source_durable = _source_health(self._durable, self._durable_channel_id)
        if source_durable is not None and source_durable.channel_id == durable.channel_id:
            durable = merge_channel_health(durable, source_durable)
        live = self._live_health.snapshot()
        source_live = _source_health(self._live, self._live_channel_id)
        if source_live is not None and source_live.channel_id == live.channel_id:
            live = merge_channel_health(live, source_live)
        return (durable, live)

    # ---- teardown -------------------------------------------------------------------

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        results = await asyncio.gather(
            self._durable.aclose(), self._live.aclose(), return_exceptions=True
        )
        first_error: BaseException | None = None
        first_cancel: BaseException | None = None
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    if first_cancel is None:
                        first_cancel = result
                elif first_error is None:
                    first_error = result
        if first_error is not None:
            raise first_error
        if first_cancel is not None:
            raise first_cancel


def _newer_fact(current: TrajectoryFact, candidate: TrajectoryFact) -> TrajectoryFact:
    """Higher revision wins; ties prefer terminal status, then the durable fact."""
    if candidate.revision > current.revision:
        return candidate
    if candidate.revision < current.revision:
        return current
    candidate_terminal = candidate.status in _TERMINAL_FACT_STATUSES
    current_terminal = current.status in _TERMINAL_FACT_STATUSES
    if candidate_terminal and not current_terminal:
        return candidate
    return current


def _newer_event(current: Event, candidate: Event) -> Event:
    """Higher revision wins; ties keep the earlier (durable) event."""
    if candidate.revision > current.revision:
        return candidate
    return current


def _source_health(source: Source, channel_id: str) -> ChannelHealth | None:
    snapshot = getattr(source, "health_snapshot", None)
    if not callable(snapshot):
        return None
    try:
        health = snapshot()
    except Exception:
        return None
    if not isinstance(health, Sequence) or not all(
        isinstance(item, ChannelHealth) for item in health
    ):
        return None
    return next((item for item in health if item.channel_id == channel_id), None)


def _type_name(value: object) -> str:
    name = type(value).__name__
    return name if _SAFE_TYPE_NAME.fullmatch(name) else "object"


__all__ = ["HybridSource", "HybridSourceError"]
