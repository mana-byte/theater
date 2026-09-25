"""Batch application glue: feed the reducer, persist source checkpoints, settle status."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from theater.daemon.observation.failures import FailureTracker
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.observation.reducer import QuietClock, Reducer
from theater.daemon.observation.turns import Turn, TurnAccumulator
from theater.harness import Event, HarnessObserver
from theater.harness.source import Batch, Source
from theater.models import Status

if TYPE_CHECKING:
    from theater.daemon.store import Store

logger = logging.getLogger("theater.observer")


class BatchApplication:
    if TYPE_CHECKING:
        _failures: FailureTracker
        _reducer: Reducer
        store: Store
        _accept_attachment: Callable[..., Any]
        _answer_turn: Callable[..., Any]
        _confirm_identity_loss: Callable[..., Any]
        _evidence_is_bound_to_another_live_participant: Callable[..., Any]
        _finish: Callable[..., Any]
        _is_untrusted_rotation: Callable[..., Any]
        mark_transcript_identity_lost: Callable[..., Any]
        _rescue_jobs: Callable[..., Any]
        _reset_identity_loss_confirmation: Callable[..., Any]
        _validate_batch: Callable[..., Any]

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
        return self._reducer.apply(
            pid,
            batch,
            clock,
            turns,
            answer_turn_fn=self._answer_turn,
            settle_fn=self._settle,
            turn_result_fn=self._turn_result,
        )

    def _apply_source_batch(
        self,
        pid: str,
        source: Source,
        batch: Batch,
        clock: QuietClock,
        turns: TurnAccumulator,
        *,
        registration: LiveRegistration | None = None,
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
        answer_turn_fn = partial(self._answer_turn, registration=registration)
        try:
            result = self._reducer.apply(
                pid,
                batch,
                clock,
                turns,
                answer_turn_fn=answer_turn_fn,
                settle_fn=self._settle,
                turn_result_fn=self._turn_result,
                path_target_fn=path_target_fn,
            )
        except Exception:
            source.rollback_source_checkpoint()
            raise
        if not batch.terminal_evidence:
            self._persist_pending_source_checkpoint(pid, source)
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
    ) -> bool:
        """Apply an inner (quiet-time) batch and remember it for evidence routing."""
        routed_batches.append(batch)
        return self._apply_source_batch(pid, source, batch, clock, turns, registration=registration)

    def _persist_pending_source_checkpoint(self, pid: str, source: Source) -> bool:
        pending_evidence = getattr(source, "pending_terminal_evidence", None)
        if callable(pending_evidence) and pending_evidence():
            # Exact terminal evidence owns this checkpoint's acknowledgement:
            # until the evidence sink has durably routed every held outcome,
            # the cursor stays unacknowledged and the evidence stays retained,
            # so acknowledging here can never drop it.
            return True
        checkpoint = source.pending_source_checkpoint()
        if checkpoint is None:
            return True
        try:
            self.store.set_source_checkpoint(pid, checkpoint)
        except Exception:
            logger.exception("persisting source checkpoint for %s failed", pid)
            return False
        source.acknowledge_source_checkpoint()
        return True

    def _unblock_on_semantic_progress(self, pid: str, batch: Batch) -> None:
        self._reducer.unblock_on_semantic_progress(pid, batch)

    async def _on_progress(
        self, pid: str, observer: HarnessObserver, batch: Batch, clock: QuietClock
    ) -> None:
        await self._reducer.on_progress(pid, observer, batch, clock)

    def _turn_result(self, event, turn: Turn) -> tuple[str, str | object | None]:
        return self._reducer.turn_result(event, turn)

    def _unblock(self, pid: str) -> None:
        self._reducer._unblock(pid)

    async def _on_quiet(
        self,
        pid: str,
        observer: HarnessObserver,
        source: Source,
        clock: QuietClock,
        turns: TurnAccumulator,
        *,
        source_status: Status | None = None,
    ) -> None:
        await self._reducer.on_quiet(
            pid,
            observer,
            source,
            clock,
            turns,
            source_status=source_status,
            validate_batch_fn=self._validate_batch,
            report_source_error_fn=lambda p, b: self._failures.report_source_error(
                p, b, finish_fn=self._finish
            ),
            accept_attachment_fn=self._accept_attachment,
            apply_fn=lambda p, b, c, t: self._apply_source_batch(p, source, b, c, t),
            on_progress_fn=self._reducer.on_progress,
            evidence_bound_fn=self._evidence_is_bound_to_another_live_participant,
            confirm_identity_loss_fn=self._confirm_identity_loss,
            mark_identity_lost_fn=self.mark_transcript_identity_lost,
            reset_identity_loss_fn=self._reset_identity_loss_confirmation,
            is_untrusted_rotation_fn=self._is_untrusted_rotation,
            rescue_jobs_fn=self._rescue_jobs,
        )

    def _settle_from_event(
        self,
        pid: str,
        event: Event,
        *,
        registration: LiveRegistration | None = None,
    ) -> None:
        self._reducer.settle_from_event(
            pid,
            event,
            answer_turn_fn=partial(self._answer_turn, registration=registration),
            turn_result_fn=self._turn_result,
        )

    def _settle(self, pid: str, desired: Status) -> None:
        self._reducer.settle(pid, desired)
