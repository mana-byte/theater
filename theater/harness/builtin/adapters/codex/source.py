from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from theater.harness.source import TranscriptSource
from theater.provenance import TranscriptProvenance

if TYPE_CHECKING:
    from .observer import CodexObserver


def _open_codex_source(
    reader: CodexObserver,
    *,
    cwd: str | None,
    session_id: str | None = None,
    after: float | None = None,
    session_provenance: str | TranscriptProvenance | None = None,
    known_location: str | None = None,
) -> _CodexSource:
    return _CodexSource(
        reader,
        cwd=cwd,
        session_id=session_id,
        after=after,
        session_provenance=session_provenance,
        known_location=known_location,
    )


class _CodexSource(TranscriptSource):
    """Report per-location process proof for Codex rollouts."""

    def __init__(self, observer: CodexObserver, **kwargs) -> None:
        super().__init__(observer, **kwargs)
        self._codex = observer

    def correlation_for(self, path: Path, session_id: str | None) -> str:
        if self._codex.proved(path):
            return str(TranscriptProvenance.PROVEN)
        return super().correlation_for(path, session_id)

    def commit_attachment(self) -> None:
        super().commit_attachment()
        self._codex._session_exact = self._session_provenance is TranscriptProvenance.EXACT

    def _prepare_history_parse(self, fh: BinaryIO, start: int) -> None:
        self._codex._seed_history_context(fh, start)
