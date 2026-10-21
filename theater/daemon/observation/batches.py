"""Batch application glue: feed the reducer, persist source checkpoints, settle status."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from theater.daemon.observation.completion_gate import BoundCompletionGate
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.observation.reducer import QuietClock, Reducer
from theater.daemon.observation.rollback import rollback_observation_state
from theater.daemon.observation.turns import Turn, TurnAccumulator
from theater.daemon.persistence.transactions import active_write_unit
from theater.harness import Event, HarnessObserver
from theater.harness.source import Batch, Source
from theater.models import Status, now

if TYPE_CHECKING:
    from theater.daemon.store import Store

logger = logging.getLogger("theater.observer")


class _NoopSource(Source):
    """A stateless source: checkpoints and evidence lifecycle stay at their defaults."""

    async def read(self) -> Batch:
        return Batch()


#: The source for callers that have no source of their own (legacy test entry points);
#: production callers pass the watched participant's real source.
_SOURCELESS = _NoopSource()


class BatchApplication:
    if TYPE_CHECKING:
        _reducer: Reducer
        store: Store
        _bind_completion: Callable[[LiveRegistration | None], BoundCompletionGate]

    def _path_target(
        self,
        pid: str,
        event: Event,
        registration: LiveRegistration | None,
    ) -> str | None:
        """The exact job handle that owns one event's path touches.

        Fails closed to ``None`` rather than guessing.
        """
        if registration is None or registration.active_job_for_turn is None:
            return None
        if event.turn_id is None or registration.native_session_id is None:
            return None
        job = registration.active_job_for_turn(
            pid,
            backend_generation=registration.backend_generation,
            native_session_id=registration.native_session_id,
            native_turn_id=event.turn_id,
        )
        return job.handle if job is not None else None

    def _record_usage(self, pid: str, event: Event) -> bool:
        return self._reducer.record_usage(pid, event)

    def _apply(self, pid: str, batch: Batch, clock: QuietClock, turns: TurnAccumulator) -> bool:
        """Apply a sourceless batch through the production assembly.

        A compatibility entry for callers that hold no source; the write unit, fence
        promotion and checkpoint handling are exactly ``_apply_source_batch``'s.
        """
        return self._apply_source_batch(pid, _SOURCELESS, batch, clock, turns)

    def _apply_source_batch(
        self,
        pid: str,
        source: Source,
        batch: Batch,
        clock: QuietClock,
        turns: TurnAccumulator,
        *,
        registration: LiveRegistration | None = None,
        completion: BoundCompletionGate | None = None,
    ) -> bool:
        """Apply once, then persist its cursor without replaying applied semantics.

        With terminal evidence the checkpoint waits until the evidence is durably routed, so
        a failure in between replays it instead of dropping it.
        """
        # Only live wiring with an active-job mapper attributes exactly; passive registrations
        # and legacy wiring keep the oldest-running heuristic.
        path_target_fn = (
            (lambda current_pid, event: self._path_target(current_pid, event, registration))
            if registration is not None and registration.active_job_for_turn is not None
            else None
        )
        answer_turn_fn = (
            completion if completion is not None else self._bind_completion(registration)
        ).answer_turn
        promoted = turns.promotions(batch.turn_state)
        try:
            if (
                self._reducer.is_write_free(batch)
                and source.pending_source_checkpoint() is None
                and not promoted
            ):
                # Nothing to write or acknowledge: skip the unit; apply fast-paths itself.
                return self._reducer.apply(
                    pid,
                    batch,
                    clock,
                    turns,
                    answer_turn_fn=answer_turn_fn,
                    settle_fn=self._settle,
                    turn_result_fn=self._turn_result,
                    path_target_fn=path_target_fn,
                )
            with (
                rollback_observation_state(clock, turns),
                self._reducer.staged_hashes(pid, batch),
                self.store.write_unit() as unit,
            ):
                if promoted:
                    # A turn born of a fenced queue item is fenced durably with this batch, so a
                    # failure leaves it unapplied and unacknowledged for the retry.
                    self.store.turn_fences.add(
                        pid, promoted, job_handle=None, created_at=now(), connection=unit.connection
                    )
                    turns.remember(promoted)
                result = self._reducer.apply(
                    pid,
                    batch,
                    clock,
                    turns,
                    answer_turn_fn=answer_turn_fn,
                    settle_fn=self._settle,
                    turn_result_fn=self._turn_result,
                    path_target_fn=path_target_fn,
                    connection=unit.connection,
                )
                if not batch.terminal_evidence:
                    self._persist_pending_source_checkpoint(pid, source, connection=unit.connection)
        except Exception:
            source.rollback_source_checkpoint()
            raise
        return result

    def _apply_and_collect(
        self,
        pid: str,
        source: Source,
        batch: Batch,
        clock: QuietClock,
        turns: TurnAccumulator,
        routed_batches: list[Batch],
        registration: LiveRegistration | None,
        completion: BoundCompletionGate,
    ) -> bool:
        """Apply an inner (quiet-time) batch and remember it for evidence routing."""
        routed_batches.append(batch)
        return self._apply_source_batch(
            pid, source, batch, clock, turns, registration=registration, completion=completion
        )

    def _persist_pending_source_checkpoint(
        self, pid: str, source: Source, *, connection=None
    ) -> bool:
        if source.pending_terminal_evidence():
            # Exact terminal evidence owns this checkpoint's acknowledgement:
            # until the evidence sink has durably routed every held outcome,
            # the cursor stays unacknowledged and the evidence stays retained,
            # so acknowledging here can never drop it.
            return True
        checkpoint = source.pending_source_checkpoint()
        if checkpoint is None:
            return True
        try:
            self.store.set_source_checkpoint(pid, checkpoint, connection=connection)
        except Exception:
            logger.exception("persisting source checkpoint for %s failed", pid)
            if active_write_unit(connection) is not None:
                raise
            return False
        unit = active_write_unit(connection)
        if unit is None:
            source.acknowledge_source_checkpoint()
        else:
            unit.after_commit(source.acknowledge_source_checkpoint)
        return True

    def _unblock_on_semantic_progress(self, pid: str, batch: Batch) -> None:
        self._reducer.unblock_on_semantic_progress(pid, batch)

    async def _on_progress(
        self, pid: str, observer: HarnessObserver, batch: Batch, clock: QuietClock
    ) -> None:
        await self._reducer.on_progress(pid, observer, batch, clock)

    def _turn_result(self, event, turn: Turn) -> tuple[str, str | object | None]:
        return self._reducer.turn_result(event, turn)

    def _settle_from_event(
        self,
        pid: str,
        event: Event,
        *,
        completion: BoundCompletionGate | None = None,
    ) -> None:
        self._reducer.settle_from_event(
            pid,
            event,
            answer_turn_fn=(
                completion if completion is not None else self._bind_completion(None)
            ).answer_turn,
            turn_result_fn=self._turn_result,
        )

    def _settle(self, pid: str, desired: Status, *, connection=None) -> None:
        self._reducer.settle(pid, desired, connection=connection)
