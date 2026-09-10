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
    ChannelKind,
)
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
        # acknowledged as durably processed. Rollback re-arms it for replay;
        # the next read replaces it, so evidence is emitted at most twice and
        # the sink's first-write-wins makes the replay harmless.
        self._held_evidence: tuple[NativeTurnOutcome, ...] = ()
        self._replay_evidence: tuple[NativeTurnOutcome, ...] = ()
        # ---- irreversible completed items ----------------------------------
        self._terminal_native_ids: OrderedDict[str, None] = OrderedDict()
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
        evidence = (*self._replay_evidence, *live.terminal_evidence)
        self._replay_evidence = ()
        self._held_evidence = evidence
        if evidence and self._wakeup is not None:
            self._wakeup.wake()
        facts = self._merge_facts(durable.trajectory, live.trajectory)
        status = self._merged_status(durable, live)
        events = (*durable.events, *live.events)
        waiting = durable.waiting and live.waiting
        progressed = durable.progressed or live.progressed or bool(evidence)
        has_more = durable.has_more or live.has_more
        return Batch(
            events=events,
            progressed=progressed,
            has_more=has_more,
            status=status,
            attached=durable.attached,
            waiting=waiting,
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

    # ---- checkpoints: durable cursor, live evidence replay -----------------------

    def source_checkpoint(self) -> str | None:
        """The persisted checkpoint is the durable reader's cursor."""
        return self._durable.source_checkpoint()

    def pending_source_checkpoint(self) -> str | None:
        return self._durable.pending_source_checkpoint()

    def acknowledge_source_checkpoint(self) -> None:
        """Both halves advance independently once the batch is durable."""
        self._held_evidence = ()
        self._replay_evidence = ()
        self._durable.acknowledge_source_checkpoint()
        with contextlib.suppress(Exception):
            self._live.acknowledge_source_checkpoint()

    def rollback_source_checkpoint(self) -> None:
        """Rewind the durable cursor and re-arm held live evidence for replay.

        Replaceable live deltas are dropped with the rolled-back batch, but
        exact terminal evidence can never be produced again, so it replays
        with the next read. The sink's first-write-wins makes the replay
        idempotent.
        """
        if self._held_evidence:
            self._replay_evidence = self._held_evidence
            logger.debug(
                "rolling back a batch with live terminal evidence; %d outcome(s) will replay",
                len(self._held_evidence),
            )
        self._durable.rollback_source_checkpoint()
        with contextlib.suppress(Exception):
            self._live.rollback_source_checkpoint()

    def arm_terminal_evidence_replay(self) -> None:
        """Re-emit held evidence after a processing failure outside apply.

        The observer calls this when the evidence sink itself raised: the
        outcomes stay pending until one routing attempt succeeds, whether or
        not the durable batch around them was applied.
        """
        if self._held_evidence:
            self._replay_evidence = self._held_evidence

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
