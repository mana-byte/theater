"""Textual messages emitted by the trajectory surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from textual.message import Message

from theater.trajectory import ParticipantLink


class ReturnToTree(Message):
    """Esc asks the owning app to return focus to the participant tree."""


class TrajectoryCopyRequested(Message):
    """Bounded trajectory detail is ready for the owning app's copy abstraction."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class TrajectoryParticipantSelected(Message):
    """A participant link asks the owning app to stage that tree leaf."""

    def __init__(
        self,
        participant_id: str,
        target_record_id: str | None = None,
        *,
        exact: bool | None = None,
        unresolved: bool = False,
        link: ParticipantLink | None = None,
    ) -> None:
        super().__init__()
        self.participant_id = participant_id
        self.target_record_id = target_record_id
        self.exact = target_record_id is not None if exact is None else exact
        self.unresolved = unresolved
        self.is_exact = self.exact
        self.is_unresolved = self.unresolved
        self.link = link


class TrajectoryBackRequested(Message):
    """The current trajectory asks the owning app to navigate back."""


class TrajectoryRetryRequested(Message):
    """A host without an injected controller can handle a retry request."""

    def __init__(self, participant_id: str) -> None:
        super().__init__()
        self.participant_id = participant_id


CopyRequest = Callable[[str], object | Awaitable[object]]
ParticipantLinkRequest = Callable[[str], object | Awaitable[object]]


__all__ = [
    "CopyRequest",
    "ParticipantLinkRequest",
    "ReturnToTree",
    "TrajectoryBackRequested",
    "TrajectoryCopyRequested",
    "TrajectoryParticipantSelected",
    "TrajectoryRetryRequested",
]
