"""Transcript observer for the writer's JSONL records, on the stock `TranscriptSource`."""

import json
from pathlib import Path

from theater.harness.contracts.callbacks import ScreenContext
from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.harness.contracts.source import Source
from theater.harness.transcript import TranscriptObserver

from .launch import stream_path


class SynthObserver(TranscriptObserver):
    def open_source_context(self, context: ParticipantObservationContext) -> Source:
        return self.open_source_for(
            participant_id=context.participant_id,
            cwd=context.cwd,
            session_id=context.session_id,
            after=context.after,
            session_provenance=context.session_provenance,
            known_location=context.known_location,
        )

    def is_idle_screen(self, capture: str) -> bool:
        return False  # never a prompt: rescue must not finish a synthetic stream

    def find_transcript(
        self, *, cwd: str, session_id: str | None = None, after=None
    ) -> Path | None:
        if not session_id:
            return None
        path = stream_path(cwd, session_id)
        return path if path.is_file() else None

    def session_id(self, transcript: Path) -> str | None:
        return transcript.stem

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        try:
            record = json.loads(line)
        except ValueError:
            return []
        text, ts = record.get("text"), record.get("ts")
        if not isinstance(text, str) or not isinstance(ts, (int, float)):
            return []
        return [Event(kind=EventKind.ASSISTANT, text=text, ts=float(ts), raw_index=index)]


def source_factory(context: ParticipantObservationContext) -> Source:
    return SynthObserver().open_source_context(context)


def classify_screen(context: ScreenContext) -> ScreenReading:
    return ScreenReading(ScreenKind.UNKNOWN, ScreenConfidence.LOW)
