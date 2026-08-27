"""Vibe transcript and usage source composition.

Vibe stores one session under
``~/.vibe/logs/session/session_<YYYYMMDD>_<HHMMSS>_<short>/messages.jsonl``
with its ``meta.json`` beside it. The directory suffix is only the first eight
characters of the native session id. Child sessions are below the parent
directory, so root ``session_*`` scans deliberately find top-level sessions.

Vibe writes no timestamps in messages JSONL. Observer timestamps are therefore
observation time, not source timing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.harness.base import Event
from theater.harness.source import Batch, Source, TranscriptSource
from theater.harness.transcript.history import HistoryReader

from .trajectory import usage_fact
from .usage import VibeUsageMixin

if TYPE_CHECKING:
    from theater.harness.builtin.adapters.vibe.observer import VibeObserver


class _VibeTranscriptSource(TranscriptSource):
    def __init__(self, observer: VibeObserver, **kwargs) -> None:
        super().__init__(observer, **kwargs)
        self._vibe = observer

    def _history_reader(self) -> HistoryReader:
        from .observer import VibeObserver

        reader = VibeObserver(
            root=self._vibe.root,
            correlation_root=self._vibe.correlation_root,
            isolated=self._vibe.isolated,
        )
        reader._cwd = self._vibe._cwd
        return HistoryReader(
            parse_record=lambda line, index: reader.parse_record(line, index, clip_text=False),
            decorate_parsed=self._decorate_parsed,
            prepare_history_parse=reader._seed_history_context,
        )

    def commit_attachment(self) -> None:
        super().commit_attachment()
        self._seed_live_context()

    def discard_attachment(self) -> None:
        super().discard_attachment()
        self._seed_live_context()

    def revoke_attachment(self) -> None:
        super().revoke_attachment()
        self._vibe._reset_turn_context()

    def _detach(self) -> None:
        super()._detach()
        self._vibe._reset_turn_context()

    def _seed_live_context(self) -> None:
        path = self.path
        if path is None:
            self._vibe._reset_turn_context()
            return
        try:
            with path.open("rb") as fh:
                self._vibe._seed_history_context(fh, self.offset)
        except OSError:
            self._vibe._reset_turn_context()


class _VibeSource(VibeUsageMixin, Source):
    """Wrap TranscriptSource with Vibe's cumulative meta usage deltas.

    Vibe stores cumulative token totals in meta.json, not per-message in
    messages.jsonl. This wrapper reads meta.json on every poll, computes the
    delta from the previous cumulative baseline, and appends a usage-only
    Event to the batch when tokens have increased.
    """

    def __init__(
        self,
        inner: TranscriptSource,
        *,
        after: float | None,
        session_id: str | None,
        known_location: str | None,
        observer: VibeObserver | None = None,
    ) -> None:
        self._inner = inner
        self._observer = observer
        self.collision_domain = inner.collision_domain
        self._init_usage(
            after=after,
            session_id=session_id,
            known_location=known_location,
        )

    @property
    def path(self) -> Path | None:
        return self._inner.path

    def correlation_for(self, path: Path, session_id: str | None) -> str:
        return self._inner.correlation_for(path, session_id)

    async def refresh(self) -> Batch:
        return await self._inner.refresh()

    async def probe_identity_loss(self):
        return await self._inner.probe_identity_loss()

    async def history(self, *, last_n: int):
        return await self._inner.history(last_n=last_n)

    async def history_page(
        self, *, before: str | None = None, limit: int = TRAJECTORY_PAGE_RECORD_LIMIT
    ):
        return await self._inner.history_page(before=before, limit=limit)

    async def aclose(self) -> None:
        await self._inner.aclose()

    def commit_attachment(self) -> None:
        self._inner.commit_attachment()
        self._clear_meta_cache()

    def discard_attachment(self) -> None:
        self._inner.discard_attachment()

    def revoke_attachment(self) -> None:
        self._inner.revoke_attachment()
        self._reset_usage()

    def admit_exact_location(self, *, location: str, session_id: str):
        result = self._inner.admit_exact_location(location=location, session_id=session_id)
        if result == "staged":
            self._clear_meta_cache()
        return result

    async def read(self) -> Batch:
        batch = await self._inner.read()
        if batch.attached is not None:
            return batch
        if self._inner.path is None:
            return batch
        usage_events = self._check_usage()
        if usage_events:
            usage_facts = tuple(
                fact for event in usage_events if (fact := self._usage_fact(event)) is not None
            )
            return replace(
                batch,
                events=[*batch.events, *usage_events],
                trajectory=(*batch.trajectory, *usage_facts),
                progressed=True,
            )
        return batch

    def _usage_fact(self, event: Event):
        turn_id = self._observer.current_turn_id if self._observer is not None else None
        return usage_fact(event, turn_id)
