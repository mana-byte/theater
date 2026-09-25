"""Exact native terminal evidence: route to the sink, retain bounded retries, release."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING

from theater.daemon.observation.live import EvidenceSink, LiveObservationHub, LiveRegistration
from theater.harness.contracts.runtime import NativeTurnOutcome
from theater.harness.source import Batch, Source, SourceContractError

logger = logging.getLogger("theater.observer")

#: Bound on retained terminal evidence per participant; when full the watch loop stops
#: pulling reads (backpressure), so exact evidence is never dropped to stay bounded.
_PENDING_EVIDENCE_MAX = 512


class TerminalEvidenceRouting:
    if TYPE_CHECKING:
        live: LiveObservationHub
        _pending_evidence: dict[
            str,
            OrderedDict[
                tuple[int, str, str], tuple[EvidenceSink | None, Source, NativeTurnOutcome]
            ],
        ]

    async def _route_terminal_evidence(
        self,
        pid: str,
        source: Source,
        batch: Batch,
        registration: LiveRegistration | None,
    ) -> bool:
        """Hand exact native terminal evidence to the registered sink (idempotent under replay).

        No sink or a raising one returns ``False`` and the observer keeps a bounded retry copy;
        the source's own copy is released only after that retry copy persists.
        """
        if not batch.terminal_evidence:
            return True
        if registration is None or registration.evidence_sink is None:
            logger.warning(
                "terminal evidence for %s has no registered evidence sink; "
                "%d outcome(s) retained for replay",
                pid,
                len(batch.terminal_evidence),
            )
            self._retain_terminal_evidence(pid, source, batch, registration)
            return False
        for outcome in batch.terminal_evidence:
            try:
                await registration.evidence_sink(
                    pid,
                    backend_generation=registration.backend_generation,
                    outcome=outcome,
                )
            except asyncio.CancelledError:
                self._retain_terminal_evidence(pid, source, batch, registration)
                raise
            except Exception:
                logger.exception(
                    "routing terminal evidence for %s failed; outcome retained for replay", pid
                )
                self._retain_terminal_evidence(pid, source, batch, registration)
                return False
        return True

    def _retain_terminal_evidence(
        self,
        pid: str,
        source: Source,
        batch: Batch,
        registration: LiveRegistration | None,
    ) -> None:
        """Retain unroutable evidence the source itself cannot replay.

        Explicit ownership handoff: a replay-capable source keeps its copy until the observer
        flushes, so teardown never acknowledges evidence before persistence.
        """
        if registration is None:
            raise SourceContractError(
                f"terminal evidence for {pid!r} has no source-bound live registration"
            )
        pending = self._pending_evidence.setdefault(pid, OrderedDict())
        additions: OrderedDict[
            tuple[int, str, str], tuple[EvidenceSink | None, Source, NativeTurnOutcome]
        ] = OrderedDict()
        for outcome in batch.terminal_evidence:
            key = (
                registration.backend_generation,
                outcome.native_session_id,
                outcome.native_turn_id,
            )
            existing = pending.get(key) or additions.get(key)
            if existing is not None:
                if existing[2] != outcome:
                    logger.error(
                        "conflicting terminal evidence for %s generation %d session %s turn %s; "
                        "keeping first observation",
                        pid,
                        registration.backend_generation,
                        outcome.native_session_id,
                        outcome.native_turn_id,
                    )
                continue
            additions[key] = (registration.evidence_sink, source, outcome)
        if len(pending) + len(additions) > _PENDING_EVIDENCE_MAX:
            raise SourceContractError(
                f"retained terminal evidence for {pid!r} would exceed the bound of "
                f"{_PENDING_EVIDENCE_MAX} outcomes"
            )
        pending.update(additions)

    def _retain_source_terminal_evidence(
        self,
        pid: str,
        source: Source,
        registration: LiveRegistration,
    ) -> None:
        """Transfer evidence consumed inside a composed read before cancellation."""
        evidence = source.terminal_evidence_snapshot()
        if not evidence:
            return
        self._retain_terminal_evidence(
            pid,
            source,
            Batch(terminal_evidence=evidence),
            registration,
        )

    async def _flush_pending_evidence(self, pid: str) -> bool:
        """Retry observer-retained terminal evidence through the sink.

        Runs before any checkpoint is acknowledged; the first failure stops the flush.
        """
        pending = self._pending_evidence.get(pid)
        if not pending:
            return True
        current = self.live.registration_for(pid)
        current_sink = None if current is None else current.evidence_sink
        for key, (bound_sink, source, outcome) in list(pending.items()):
            sink = current_sink or bound_sink
            if sink is None:
                logger.warning(
                    "retained terminal evidence for %s still has no registered evidence sink",
                    pid,
                )
                return False
            try:
                await sink(pid, backend_generation=key[0], outcome=outcome)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retrying retained terminal evidence for %s failed", pid)
                return False
            del pending[key]
            if not any(candidate_source is source for _, candidate_source, _ in pending.values()):
                # All observer-owned outcomes from this source are durable.
                # Only now may the old source release its held replay copy.
                self._ack_terminal_evidence(source)
        if not pending:
            self._pending_evidence.pop(pid, None)
        return True

    def _ack_terminal_evidence(self, source: Source) -> None:
        """Release evidence the sink accepted; the checkpoint is acked separately."""
        delivered = getattr(source, "terminal_evidence_delivered", None)
        if not callable(delivered):
            return
        try:
            delivered()
        except Exception:
            logger.debug("releasing terminal evidence failed", exc_info=True)
