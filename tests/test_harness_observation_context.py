"""Participant observation context and legacy source-factory dispatch."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from math import inf, nan

import pytest

from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.observation import HarnessObserver
from theater.harness.contracts.source import Batch, Source
from theater.harness.transcript.observer import open_participant_source
from theater.provenance import TranscriptProvenance


class _Source(Source):
    async def read(self) -> Batch:
        return Batch()


class _Observer(HarnessObserver):
    def __init__(self) -> None:
        self.source, self.opened = _Source(), None

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Source:
        self.opened = (cwd, session_id, after)
        return self.source

    def is_idle_screen(self, capture: str) -> bool:
        return False


class _ExactObserver(_Observer):
    def open_source_for(
        self,
        *,
        participant_id: str,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_exact: bool = False,
    ) -> Source:
        self.opened = (participant_id, cwd, session_id, after, session_exact)
        return self.source


class _StructuralLegacy:
    def __init__(self) -> None:
        self.source, self.opened = _Source(), None

    def open_source_for(
        self,
        *,
        participant_id: str,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: TranscriptProvenance | None = None,
        known_location: str | None = None,
        transcript_domain: str | None = None,
        pane_pid: int | None = None,
    ) -> Source:
        self.opened = (
            participant_id,
            cwd,
            session_id,
            after,
            session_provenance,
            known_location,
            transcript_domain,
            pane_pid,
        )
        return self.source


def test_context_is_frozen_and_normalizes_provenance() -> None:
    context = ParticipantObservationContext(
        "participant", None, after=3.5, session_provenance="unknown"
    )

    assert context.after == 3.5
    assert context.session_provenance is TranscriptProvenance.HEURISTIC
    with pytest.raises(FrozenInstanceError):
        context.cwd = "/other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("participant_id", 1, TypeError),
        ("participant_id", "", ValueError),
        ("participant_id", " \t", ValueError),
        ("cwd", 1, TypeError),
        ("session_id", 1, TypeError),
        ("known_location", 1, TypeError),
        ("transcript_domain", 1, TypeError),
        ("after", True, TypeError),
        ("after", "later", TypeError),
        ("after", nan, ValueError),
        ("after", inf, ValueError),
        ("after", -inf, ValueError),
        ("pane_pid", True, TypeError),
        ("pane_pid", "42", TypeError),
        ("pane_pid", 0, ValueError),
        ("pane_pid", -1, ValueError),
        ("session_provenance", 1, TypeError),
    ],
)
def test_context_rejects_invalid_values(field: str, value: object, error: type[Exception]) -> None:
    values: dict[str, object] = {"participant_id": "participant", "cwd": None, field: value}

    with pytest.raises(error):
        ParticipantObservationContext(**values)


def test_explicit_context_override_receives_normalized_context() -> None:
    class ContextObserver(_Observer):
        def open_source_context(self, context: ParticipantObservationContext) -> Source:
            self.opened = (context,)
            return self.source

    observer = ContextObserver()
    assert (
        open_participant_source(
            observer,
            participant_id="participant",
            cwd="/work",
            session_provenance="exact",
            transcript_domain="/logs",
            pane_pid=42,
        )
        is observer.source
    )
    context = observer.opened[0] if observer.opened else None
    assert isinstance(context, ParticipantObservationContext)
    assert context.session_provenance is TranscriptProvenance.EXACT
    assert (context.transcript_domain, context.pane_pid) == ("/logs", 42)


def test_inherited_default_delegates_to_open_source_for() -> None:
    observer = _Observer()
    context = ParticipantObservationContext(
        participant_id="participant",
        cwd="/work",
        session_id="session",
        after=3.5,
    )

    assert observer.open_source_context(context) is observer.source
    assert observer.opened == ("/work", "session", 3.5)


def test_legacy_session_exact_dispatch_is_preserved() -> None:
    observer = _ExactObserver()

    open_participant_source(
        observer, participant_id="participant", cwd=None, session_provenance="exact"
    )
    assert observer.opened == ("participant", None, None, None, True)


def test_structural_open_source_for_receives_optional_arguments() -> None:
    observer = _StructuralLegacy()

    open_participant_source(
        observer,
        participant_id="participant",
        cwd="/work",
        session_id="session",
        after=4,
        session_provenance="operator",
        known_location="/logs/session.jsonl",
        transcript_domain="/logs",
        pane_pid=42,
    )
    assert observer.opened == (
        "participant",
        "/work",
        "session",
        4,
        TranscriptProvenance.OPERATOR,
        "/logs/session.jsonl",
        "/logs",
        42,
    )


def test_structural_context_observer_and_old_open_source_shape() -> None:
    class ContextObserver:
        def __init__(self) -> None:
            self.source, self.context = _Source(), None

        def open_source_context(self, context: ParticipantObservationContext) -> Source:
            self.context = context
            return self.source

    class OldObserver:
        def __init__(self) -> None:
            self.source, self.opened = _Source(), None

        def open_source(
            self,
            *,
            cwd: str | None,
            session_id: str | None = None,
            after: float | None = None,
        ) -> Source:
            self.opened = (cwd, session_id, after)
            return self.source

    context_observer, old_observer = ContextObserver(), OldObserver()
    open_participant_source(
        context_observer, participant_id="participant", cwd="/work", transcript_domain="/logs"
    )
    assert context_observer.context == ParticipantObservationContext(
        participant_id="participant", cwd="/work", transcript_domain="/logs"
    )
    open_participant_source(
        old_observer,
        participant_id="participant",
        cwd="/work",
        session_id="session",
        after=5,
        known_location="/logs/session.jsonl",
        pane_pid=42,
    )
    assert old_observer.opened == ("/work", "session", 5)


def test_instance_context_override_is_selected() -> None:
    observer = _Observer()
    seen: list[ParticipantObservationContext] = []

    def open_source_context(context: ParticipantObservationContext) -> Source:
        seen.append(context)
        return observer.source

    observer.open_source_context = open_source_context  # type: ignore[method-assign]

    open_participant_source(observer, participant_id="participant", cwd=None)
    assert seen == [ParticipantObservationContext(participant_id="participant", cwd=None)]
