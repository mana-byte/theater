"""Plain values for source completion evidence and deferred-task gates."""

from __future__ import annotations

from dataclasses import dataclass

from theater.constants.handoff import PIN_DIRTY, PIN_PINNED, PIN_UNPINNED


@dataclass(frozen=True, slots=True)
class GitFacts:
    """Git state read at one instant; ``pin_state`` says whether it can be reviewed exactly."""

    pin_state: str
    commit_sha: str | None = None
    branch: str | None = None
    dirty_entries: int | None = None
    reason: str | None = None

    @classmethod
    def unpinned(cls, reason: str) -> GitFacts:
        return cls(pin_state=PIN_UNPINNED, reason=reason)

    @property
    def reviewable(self) -> bool:
        return self.pin_state == PIN_PINNED and self.commit_sha is not None

    @classmethod
    def from_status(cls, commit: str, branch: str | None, dirty_entries: int) -> GitFacts:
        state = PIN_DIRTY if dirty_entries else PIN_PINNED
        return cls(state, commit, branch, dirty_entries)


@dataclass(frozen=True, slots=True)
class JobEvidence:
    """Durable completion evidence for one job; stored with, never after, its terminal state."""

    job_handle: str
    outcome: str
    error_code: str | None
    answer: str | None
    answer_chars: int
    answer_source: str
    provenance: str | None
    harness: str | None
    participant_id: str | None
    workspace_id: str | None
    cwd: str | None
    git: GitFacts
    captured_at: float


@dataclass(frozen=True, slots=True)
class HandoffGate:
    """A deferred task waiting on ``source_handle``; ``job_handle`` is its one task handle."""

    job_handle: str
    caller_id: str | None
    source_handle: str
    source_participant_id: str | None
    source_name: str | None
    task_kind: str
    request: str
    state: str
    error_code: str | None
    error: str | None
    created_at: float
    updated_at: float
