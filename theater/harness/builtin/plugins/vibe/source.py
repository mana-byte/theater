"""Vibe transcript and usage source composition."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.harness.base import Event
from theater.harness.source import Batch, Source, TranscriptSource
from theater.harness.transcript.discovery import stateful_history_reader
from theater.provenance import TranscriptProvenance

from .trajectory import usage_fact
from .unified_source import UnifiedVibeSource
from .usage import VibeUsageMixin

if TYPE_CHECKING:
    from .observer import VibeObserver


def _open_vibe_source(
    observer: VibeObserver,
    *,
    cwd: str | None,
    session_id: str | None = None,
    after: float | None = None,
    session_provenance: str | TranscriptProvenance | None = None,
    known_location: str | None = None,
    source_checkpoint: str | None = None,
) -> _VibeSource:
    from .observer import VibeObserver

    reader = VibeObserver(
        root=observer.root,
        correlation_root=observer.correlation_root,
        isolated=observer.isolated,
    )
    reader._cwd = cwd
    inner = _VibeTranscriptSource(
        reader,
        cwd=cwd,
        session_id=session_id,
        after=after,
        allow_refresh=True,
        exact_attachments=reader.isolated,
        session_provenance=session_provenance,
        collision_domain=str(reader.root.resolve()),
        known_location=known_location,
    )
    return _VibeSource(
        inner,
        after=after,
        session_id=session_id,
        known_location=known_location,
        observer=reader,
        cwd=cwd,
        session_provenance=session_provenance,
        source_checkpoint=source_checkpoint,
    )


class _VibeTranscriptSource(TranscriptSource):
    if TYPE_CHECKING:
        _observer: VibeObserver

    async def _locate(self, *, session_id: str | None) -> Path | None:
        """Keep the append-only parser away from Unified's mutable CURRENT file."""
        return await asyncio.to_thread(
            self._observer.find_legacy_transcript,
            cwd=self._cwd,
            session_id=session_id,
            after=self._after,
        )

    def _history_reader(self):
        from .observer import VibeObserver

        def _clone():
            reader = VibeObserver(
                root=self._observer.root,
                correlation_root=self._observer.correlation_root,
                isolated=self._observer.isolated,
            )
            reader._cwd = self._observer._cwd
            return reader

        return stateful_history_reader(
            clone=_clone,
            seed_of=lambda r: r._seed_history_context,
            decorate=self._decorate_parsed,
        )

    def commit_attachment(self) -> None:
        super().commit_attachment()
        self._seed_live_context()

    def discard_attachment(self) -> None:
        super().discard_attachment()
        self._seed_live_context()

    def revoke_attachment(self) -> None:
        super().revoke_attachment()
        self._observer._reset_turn_context()

    def _detach(self) -> None:
        super()._detach()
        self._observer._reset_turn_context()

    def _seed_live_context(self) -> None:
        path = self.path
        if path is None:
            self._observer._reset_turn_context()
            return
        try:
            with path.open("rb") as fh:
                self._observer._seed_history_context(fh, self.offset)
        except OSError:
            self._observer._reset_turn_context()


class _VibeSource(VibeUsageMixin, Source):
    """Route legacy JSONL and Unified stores behind one source contract."""

    def __init__(
        self,
        inner: Source,
        *,
        after: float | None,
        session_id: str | None,
        known_location: str | None,
        observer: VibeObserver | None = None,
        cwd: str | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        source_checkpoint: str | None = None,
    ) -> None:
        self._inner = inner
        self._observer = observer
        self._cwd = cwd
        self._after = after
        self._session_id = session_id
        self._session_provenance = session_provenance
        self._known_location = known_location
        self._source_checkpoint = source_checkpoint
        self.collision_domain = inner.collision_domain
        self._init_usage(
            after=after,
            session_id=session_id,
            known_location=known_location,
        )
        if observer is not None and known_location is not None:
            self._select_path(Path(known_location))

    @staticmethod
    def _is_unified_path(path: Path) -> bool:
        return path.name == "CURRENT" and path.parent.parent.name == "unified"

    def _select_path(self, path: Path) -> None:
        is_unified = self._is_unified_path(path)
        if is_unified and not isinstance(self._inner, UnifiedVibeSource):
            assert self._observer is not None
            self._inner = UnifiedVibeSource(
                self._observer,
                cwd=self._cwd,
                session_id=self._session_id,
                after=self._after,
                session_provenance=self._session_provenance,
                known_location=str(path),
                source_checkpoint=self._source_checkpoint,
            )
        elif not is_unified and isinstance(self._inner, UnifiedVibeSource):
            assert self._observer is not None
            self._inner = _VibeTranscriptSource(
                self._observer,
                cwd=self._cwd,
                session_id=self._session_id,
                after=self._after,
                allow_refresh=True,
                exact_attachments=self._observer.isolated,
                session_provenance=self._session_provenance,
                collision_domain=str(self._observer.root.resolve()),
                known_location=str(path),
            )
        self.collision_domain = self._inner.collision_domain

    async def _select_discovered_backend(self) -> None:
        if self._observer is None or self.path is not None:
            return
        path = await asyncio.to_thread(
            self._observer.find_transcript,
            cwd=self._cwd,
            session_id=self._session_id,
            after=self._after,
        )
        if path is not None:
            self._select_path(path)

    @property
    def path(self) -> Path | None:
        value = getattr(self._inner, "path", None)
        return value if isinstance(value, Path) else None

    def correlation_for(self, path: Path, session_id: str | None) -> str:
        correlation = getattr(self._inner, "correlation_for", None)
        if callable(correlation):
            return correlation(path, session_id)
        return str(TranscriptProvenance.HEURISTIC)

    async def refresh(self) -> Batch:
        await self._select_discovered_backend()
        return await self._inner.refresh()

    async def probe_identity_loss(self):
        return await self._inner.probe_identity_loss()

    def health_snapshot(self):
        return self._inner.health_snapshot()

    async def history(self, *, last_n: int):
        await self._select_discovered_backend()
        return await self._inner.history(last_n=last_n)

    async def history_page(
        self,
        *,
        before: str | None = None,
        snapshot: str | None = None,
        limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
        include_full_text: bool = False,
    ):
        await self._select_discovered_backend()
        return await self._inner.history_page(
            before=before,
            snapshot=snapshot,
            limit=limit,
            include_full_text=include_full_text,
        )

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
        if self._observer is not None:
            self._session_id = session_id
            self._session_provenance = TranscriptProvenance.EXACT
            self._known_location = location
            self._select_path(Path(location))
        result = self._inner.admit_exact_location(location=location, session_id=session_id)
        if result == "staged":
            self._clear_meta_cache()
        return result

    async def read(self) -> Batch:
        await self._select_discovered_backend()
        batch = await self._inner.read()
        if batch.attached is not None:
            return batch
        if self.path is None:
            return batch
        if isinstance(self._inner, UnifiedVibeSource):
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

    def source_checkpoint(self) -> str | None:
        return self._inner.source_checkpoint()

    def pending_source_checkpoint(self) -> str | None:
        return self._inner.pending_source_checkpoint()

    def acknowledge_source_checkpoint(self) -> None:
        self._inner.acknowledge_source_checkpoint()

    def rollback_source_checkpoint(self) -> None:
        self._inner.rollback_source_checkpoint()
