"""Path touches resolve to the exact job that owns the event's turn."""

from __future__ import annotations

from types import SimpleNamespace

from tests.support.observation import LIVE_CHANNEL
from theater.daemon.observation.batches import BatchApplication
from theater.daemon.observation.live import LiveRegistration
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.runtime import LiveChannelDeclaration
from theater.harness.source import Batch, Source


class _LiveSource(Source):
    def __init__(self, native_turn_id: str | None = None) -> None:
        self._native_turn_id = native_turn_id

    async def read(self) -> Batch:
        return Batch()

    def active_native_turn_id(self) -> str | None:
        return self._native_turn_id


class _PlainSource(Source):
    async def read(self) -> Batch:
        return Batch()


def _registration(source: Source, lookup) -> LiveRegistration:
    return LiveRegistration(
        participant_id="p1",
        live_source=source,
        channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
        backend_generation=1,
        native_session_id="session",
        active_job_for_turn=lookup,
    )


def _lookup(handles: dict[str, str]):
    def lookup(_pid, *, backend_generation, native_session_id, native_turn_id):
        del backend_generation, native_session_id
        handle = handles.get(native_turn_id)
        return SimpleNamespace(handle=handle) if handle is not None else None

    return lookup


def test_path_target_falls_back_to_the_active_native_turn():
    """A transcript turn id that resolves no job falls back to the live native turn."""
    app = BatchApplication()
    registration = _registration(_LiveSource("native-turn"), _lookup({"native-turn": "native-job"}))
    event = Event(kind=EventKind.ASSISTANT, text="wrote a file", turn_id="msg_011Cgp")
    assert app._path_target("p1", event, registration) == "native-job"


def test_path_target_prefers_the_event_turn_id():
    """The exact transcript turn id wins when it resolves a job."""
    app = BatchApplication()
    registration = _registration(
        _LiveSource("native-turn"), _lookup({"turn-1": "exact-job", "native-turn": "native-job"})
    )
    event = Event(kind=EventKind.ASSISTANT, text="wrote a file", turn_id="turn-1")
    assert app._path_target("p1", event, registration) == "exact-job"


def test_path_target_without_active_native_turn_returns_none():
    """A source that does not report a native turn stays exact (Codex behaviour)."""
    app = BatchApplication()
    registration = _registration(_PlainSource(), _lookup({"native-turn": "native-job"}))
    event = Event(kind=EventKind.ASSISTANT, text="wrote a file", turn_id="msg_011Cgp")
    assert app._path_target("p1", event, registration) is None


def test_path_target_without_event_turn_id_still_falls_back():
    """An event with no turn id is credited to the active native turn."""
    app = BatchApplication()
    registration = _registration(_LiveSource("native-turn"), _lookup({"native-turn": "native-job"}))
    event = Event(kind=EventKind.ASSISTANT, text="wrote a file", turn_id=None)
    assert app._path_target("p1", event, registration) == "native-job"
