"""Participant observation context and legacy source-factory dispatch."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.observation import HarnessObserver
from theater.harness.contracts.source import Batch, Source
from theater.harness.transcript.observer import open_participant_source
from theater.provenance import TranscriptProvenance


class _Source(Source):
    async def read(self) -> Batch:
        return Batch()


class _InheritedObserver(HarnessObserver):
    def __init__(self) -> None:
        self.source = _Source()
        self.calls: list[tuple[str | None, str | None, float | None]] = []

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Source:
        self.calls.append((cwd, session_id, after))
        return self.source

    def is_idle_screen(self, capture: str) -> bool:
        return False


def test_context_is_frozen_and_normalizes_provenance() -> None:
    context = ParticipantObservationContext(
        participant_id="participant",
        cwd=None,
        session_provenance="not-a-provenance",
    )

    assert context.session_provenance is TranscriptProvenance.HEURISTIC
    with pytest.raises(FrozenInstanceError):
        context.cwd = "/other"  # type: ignore[misc]


def test_explicit_context_method_receives_one_normalized_context() -> None:
    class ContextObserver(_InheritedObserver):
        def __init__(self) -> None:
            super().__init__()
            self.contexts: list[ParticipantObservationContext] = []

        def open_source_context(self, context: ParticipantObservationContext) -> Source:
            self.contexts.append(context)
            return self.source

    observer = ContextObserver()
    source = open_participant_source(
        observer,
        participant_id="participant",
        cwd="/work",
        session_id="session",
        after=12,
        session_provenance="exact",
        known_location="/logs/session.jsonl",
        transcript_domain="/logs",
        pane_pid=42,
    )

    assert source is observer.source
    assert observer.calls == []
    assert observer.contexts == [
        ParticipantObservationContext(
            participant_id="participant",
            cwd="/work",
            session_id="session",
            after=12,
            session_provenance=TranscriptProvenance.EXACT,
            known_location="/logs/session.jsonl",
            transcript_domain="/logs",
            pane_pid=42,
        )
    ]


def test_inherited_context_default_keeps_open_source_for_compatibility() -> None:
    observer = _InheritedObserver()
    context = ParticipantObservationContext(participant_id="participant", cwd="/work", after=3)

    assert observer.open_source_context(context) is observer.source
    assert observer.calls == [("/work", None, 3)]

    assert (
        open_participant_source(observer, participant_id="participant", cwd="/work")
        is observer.source
    )
    assert observer.calls[-1] == ("/work", None, None)


def test_base_context_default_delegates_to_standard_open_source_for() -> None:
    class StandardObserver(_InheritedObserver):
        def __init__(self) -> None:
            super().__init__()
            self.kwargs: dict[str, object] | None = None

        def open_source_for(
            self,
            *,
            participant_id: str,
            cwd: str | None,
            session_id: str | None = None,
            after: float | None = None,
            session_provenance: str | TranscriptProvenance | None = None,
            known_location: str | None = None,
        ) -> Source:
            self.kwargs = {
                "participant_id": participant_id,
                "cwd": cwd,
                "session_id": session_id,
                "after": after,
                "session_provenance": session_provenance,
                "known_location": known_location,
            }
            return self.source

    observer = StandardObserver()
    context = ParticipantObservationContext(
        participant_id="participant",
        cwd="/work",
        session_id="session",
        after=3.5,
        session_provenance="exact",
        known_location="/logs/session.jsonl",
    )

    assert observer.open_source_context(context) is observer.source
    assert observer.kwargs == {
        "participant_id": "participant",
        "cwd": "/work",
        "session_id": "session",
        "after": 3.5,
        "session_provenance": TranscriptProvenance.EXACT,
        "known_location": "/logs/session.jsonl",
    }


def test_inherited_context_default_does_not_bypass_legacy_session_exact() -> None:
    class LegacyObserver(_InheritedObserver):
        def __init__(self) -> None:
            super().__init__()
            self.exact: bool | None = None

        def open_source_for(
            self,
            *,
            participant_id: str,
            cwd: str | None,
            session_id: str | None = None,
            after: float | None = None,
            session_exact: bool = False,
        ) -> Source:
            self.exact = session_exact
            return self.source

    observer = LegacyObserver()

    assert (
        open_participant_source(
            observer,
            participant_id="participant",
            cwd=None,
            session_provenance="exact",
        )
        is observer.source
    )
    assert observer.exact is True


def test_structural_legacy_observer_uses_open_source_for() -> None:
    class StructuralObserver:
        def __init__(self) -> None:
            self.source = _Source()
            self.participant_id: str | None = None

        def open_source_for(
            self,
            *,
            participant_id: str,
            cwd: str | None,
            session_id: str | None = None,
            after: float | None = None,
        ) -> Source:
            self.participant_id = participant_id
            return self.source

        def open_source(
            self,
            *,
            cwd: str | None,
            session_id: str | None = None,
            after: float | None = None,
        ) -> Source:
            raise AssertionError("open_source_for should be selected")

    observer = StructuralObserver()

    assert (
        open_participant_source(observer, participant_id="participant", cwd=None) is observer.source
    )
    assert observer.participant_id == "participant"


def test_structural_context_observer_receives_the_context() -> None:
    class StructuralContextObserver:
        def __init__(self) -> None:
            self.source = _Source()
            self.context: ParticipantObservationContext | None = None

        def open_source_context(self, context: ParticipantObservationContext) -> Source:
            self.context = context
            return self.source

    observer = StructuralContextObserver()

    assert (
        open_participant_source(
            observer,
            participant_id="participant",
            cwd="/work",
            transcript_domain="/logs",
        )
        is observer.source
    )
    assert observer.context is not None
    assert observer.context.transcript_domain == "/logs"


def test_legacy_session_exact_and_optional_arguments_keep_their_shapes() -> None:
    class ExactObserver:
        def __init__(self) -> None:
            self.source = _Source()
            self.exact: bool | None = None

        def open_source_for(
            self,
            *,
            participant_id: str,
            cwd: str | None,
            session_id: str | None = None,
            after: float | None = None,
            session_exact: bool = False,
        ) -> Source:
            self.exact = session_exact
            return self.source

    class ExtraObserver:
        def __init__(self) -> None:
            self.source = _Source()
            self.kwargs: dict[str, object] | None = None

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
            self.kwargs = {
                "participant_id": participant_id,
                "cwd": cwd,
                "session_id": session_id,
                "after": after,
                "session_provenance": session_provenance,
                "known_location": known_location,
                "transcript_domain": transcript_domain,
                "pane_pid": pane_pid,
            }
            return self.source

    exact = ExactObserver()
    extra = ExtraObserver()

    assert (
        open_participant_source(
            exact,
            participant_id="participant",
            cwd=None,
            session_provenance="exact",
        )
        is exact.source
    )
    assert exact.exact is True
    assert (
        open_participant_source(
            extra,
            participant_id="participant",
            cwd="/work",
            session_id="session",
            after=4,
            session_provenance="operator",
            known_location="/logs/session.jsonl",
            transcript_domain="/logs",
            pane_pid=42,
        )
        is extra.source
    )
    assert extra.kwargs == {
        "participant_id": "participant",
        "cwd": "/work",
        "session_id": "session",
        "after": 4,
        "session_provenance": TranscriptProvenance.OPERATOR,
        "known_location": "/logs/session.jsonl",
        "transcript_domain": "/logs",
        "pane_pid": 42,
    }


def test_old_plugin_open_source_call_shape_is_unchanged() -> None:
    class OldPluginObserver:
        def __init__(self) -> None:
            self.source = _Source()
            self.kwargs: dict[str, object] | None = None

        def open_source(
            self,
            *,
            cwd: str | None,
            session_id: str | None = None,
            after: float | None = None,
        ) -> Source:
            self.kwargs = {"cwd": cwd, "session_id": session_id, "after": after}
            return self.source

    observer = OldPluginObserver()

    assert (
        open_participant_source(
            observer,
            participant_id="participant",
            cwd="/work",
            session_id="session",
            after=5,
            known_location="/logs/session.jsonl",
            pane_pid=42,
        )
        is observer.source
    )
    assert observer.kwargs == {"cwd": "/work", "session_id": "session", "after": 5}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"participant_id": 1, "cwd": None},
        {"participant_id": "participant", "cwd": 1},
        {"participant_id": "participant", "cwd": None, "session_id": 1},
        {"participant_id": "participant", "cwd": None, "known_location": 1},
        {"participant_id": "participant", "cwd": None, "transcript_domain": 1},
        {"participant_id": "participant", "cwd": None, "after": "later"},
        {"participant_id": "participant", "cwd": None, "pane_pid": "42"},
        {"participant_id": "participant", "cwd": None, "session_provenance": 1},
    ],
)
def test_context_rejects_invalid_primitives(kwargs: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        ParticipantObservationContext(**kwargs)


@pytest.mark.parametrize("after", [3, 3.5])
def test_context_preserves_int_and_float_after(after: int | float) -> None:
    context = ParticipantObservationContext(participant_id="participant", cwd=None, after=after)

    assert context.after == after


def test_instance_context_override_is_selected() -> None:
    observer = _InheritedObserver()
    seen: list[ParticipantObservationContext] = []

    def open_source_context(context: ParticipantObservationContext) -> Source:
        seen.append(context)
        return observer.source

    observer.open_source_context = open_source_context  # type: ignore[method-assign]

    assert (
        open_participant_source(observer, participant_id="participant", cwd=None) is observer.source
    )
    assert seen[0].participant_id == "participant"
