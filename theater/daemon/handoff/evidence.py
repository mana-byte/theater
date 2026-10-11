"""Compose and write a source job's completion evidence with its terminal state."""

from __future__ import annotations

from theater.constants.handoff import ANSWER_MAX_CHARS
from theater.daemon.handoff.gitfacts import pin_git_facts
from theater.daemon.handoff.models import GitFacts, JobEvidence
from theater.harness.contracts.runtime import ControlDeliveryPhase, DeliveryResult
from theater.models import Job, WorkspaceOwnershipKind, now

_NO_ANSWER = "none"


class EvidencePinner:
    """Decides which jobs leave evidence, pins their Git state, and builds the record.

    Only jobs a handoff can name are pinned: a source with a waiting gate, or any job of a
    Theater-owned isolated worktree. Everything else is honestly "no evidence recorded".
    """

    def __init__(self, store) -> None:
        self._store = store

    def delivered(self, job: Job) -> bool:
        """Whether the job's prompt may have reached the target; a queued or refused one never did.

        Only a job with a real chance of completing needs Git read; cancelling, interrupting or
        failing queued work must cost nothing. A job with no control record (a spawn, a legacy
        send) counts as delivered.
        """
        if job.kind != "send":
            return True
        operations = self._store.control_operations_for_job(job.handle)
        if not operations:
            return True
        return any(
            operation.delivery_phase is ControlDeliveryPhase.DISPATCHED
            or (
                operation.delivery_phase is ControlDeliveryPhase.SETTLED
                and operation.delivery_result is not DeliveryResult.REJECTED
            )
            for operation in operations
        )

    def required(self, job: Job) -> bool:
        if job.target_id is None or not self.delivered(job):
            return False
        if self._store.handoff_gates(("waiting", "opening"), source_handle=job.handle):
            return True
        participant = self._store.get_participant(job.target_id)
        if participant is None or participant.workspace_id is None:
            return False
        workspace = self._store.workspaces.get(participant.workspace_id)
        return workspace is not None and workspace.ownership_kind == (
            WorkspaceOwnershipKind.THEATER.value
        )

    def workspace_of(self, job: Job) -> str | None:
        participant = self._store.get_participant(job.target_id) if job.target_id else None
        return participant.cwd if participant is not None else None

    def capture(self, job: Job) -> GitFacts:
        """Pin the target's Git state now; call it before any write unit opens."""
        return pin_git_facts(self.workspace_of(job))

    def build(
        self,
        job: Job,
        git: GitFacts,
        *,
        state: str,
        result: str | None,
        error_code: str | None,
        raw_result: object,
    ) -> JobEvidence:
        text, source = _NO_ANSWER, _NO_ANSWER
        answer = None
        if isinstance(raw_result, str) and raw_result:
            answer, source = raw_result, "raw"
        elif result:
            answer, source = result, "result"
        chars = len(answer) if answer else 0
        participant = self._store.get_participant(job.target_id) if job.target_id else None
        return JobEvidence(
            job_handle=job.handle,
            outcome=state,
            error_code=error_code,
            answer=answer[:ANSWER_MAX_CHARS] if answer else None,
            answer_chars=chars,
            answer_source=source if answer else text,
            provenance=participant.session_correlation if participant is not None else None,
            harness=participant.harness if participant is not None else None,
            participant_id=job.target_id,
            workspace_id=participant.workspace_id if participant is not None else None,
            cwd=participant.cwd if participant is not None else None,
            git=git,
            captured_at=now(),
        )
