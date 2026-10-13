"""Seamless send: deliver once when safe, else queue durably under the same handle."""

from __future__ import annotations

from collections.abc import Callable

from theater.constants.refusals import NEXT_SELF_SEND
from theater.daemon.controls._common import QUEUE_ORIGIN_SEND
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.dispatch import TEMPORARY_REFUSALS
from theater.daemon.handoff.models import HandoffGate
from theater.harness.contracts.runtime import ControlDeliveryPhase
from theater.models import HumanPresent, Job, JobState

OUTCOME_DELIVERED = "delivered"
OUTCOME_QUEUED = "queued"
OUTCOME_GATED = "gated"


class MessageControls(ControlHost):
    """One public ``send`` for peers; the caller never chooses how a busy target is handled."""

    async def send_when_safe(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
    ) -> tuple[Job, bool]:
        """Return ``(job, queued)``; permanent refusals raise before anything is accepted.

        A temporary refusal (busy, attended, awaiting a decision, repair fence) leaves nothing
        durable, so queueing afterwards mints the one and only handle. Delivery of a queued
        message re-gates fresh presence, and an uncertain delivery is never replayed.
        """
        try:
            job = await self.send(
                participant_id,
                caller_id=caller_id,
                prompt=prompt,
                response_format=response_format,
                on_reserved=on_reserved,
                defer_provider_presence=caller_id != participant_id,
            )
        except TEMPORARY_REFUSALS:
            if caller_id == participant_id:
                raise  # a self-send keeps ordinary send's refusal; it is never queued
            job = await self.queue_followup(
                participant_id,
                caller_id=caller_id,
                prompt=prompt,
                response_format=response_format,
                on_reserved=on_reserved,
                origin=QUEUE_ORIGIN_SEND,
            )
            self._count_send_admission(OUTCOME_QUEUED)
            return job, True
        if (
            caller_id == participant_id
            and job.state == JobState.CRASHED
            and job.error_code == "human_present"
        ):
            raise HumanPresent(
                job.result or "human present at provider delivery",
                details={"next": NEXT_SELF_SEND},
            )
        queued = any(
            operation.delivery_phase is ControlDeliveryPhase.QUEUED
            for operation in self._store.control_operations_for_job(job.handle)
        )
        self._count_send_admission(OUTCOME_QUEUED if queued else OUTCOME_DELIVERED)
        return job, queued

    async def queue_gated_send(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None,
        gate: HandoffGate,
    ) -> Job:
        """Queue a message that waits for a source job: always queued, never delivered now."""
        job = await self.queue_followup(
            participant_id,
            caller_id=caller_id,
            prompt=prompt,
            response_format=response_format,
            origin=QUEUE_ORIGIN_SEND,
            gate=gate,
        )
        self._count_send_admission(OUTCOME_GATED)
        return job
