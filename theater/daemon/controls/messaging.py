"""Seamless send: deliver once when safe, else queue durably under the same handle."""

from __future__ import annotations

from theater.daemon.controls._common import QUEUE_ORIGIN_SEND
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.dispatch import TEMPORARY_REFUSALS
from theater.models import Job

OUTCOME_DELIVERED = "delivered"
OUTCOME_QUEUED = "queued"


class MessageControls(ControlHost):
    """One public ``send`` for peers; the caller never chooses how a busy target is handled."""

    async def send_when_safe(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None = None,
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
            )
        except TEMPORARY_REFUSALS:
            if caller_id == participant_id:
                raise  # a self-send keeps ordinary send's refusal; it is never queued
            job = await self.queue_followup(
                participant_id,
                caller_id=caller_id,
                prompt=prompt,
                response_format=response_format,
                origin=QUEUE_ORIGIN_SEND,
            )
            self._count_send_admission(OUTCOME_QUEUED)
            return job, True
        self._count_send_admission(OUTCOME_DELIVERED)
        return job, False
