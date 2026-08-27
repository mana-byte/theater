from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from theater.harness.source import TranscriptSource
from theater.provenance import TranscriptProvenance

if TYPE_CHECKING:
    from .observer import CodexObserver


class _CodexSource(TranscriptSource):
    """A codex transcript source whose exactness is decided per location.

    The flags `TranscriptSource` already understands are fixed when the source
    is built: either every candidate under this root has one owner, or the
    session id we were handed was itself exact. Neither describes codex, where
    the same source proves ownership on one poll — the process was holding the
    file — and can only guess on the next, because `lsof` is missing or the
    rollout does not exist yet. So the question is asked about the path.
    """

    def __init__(self, observer: CodexObserver, **kwargs) -> None:
        super().__init__(observer, **kwargs)
        #: Same as `self._observer`, renamed so `proved` is not a `TranscriptObserver` API.
        self._codex = observer

    def correlation_for(self, path: Path, session_id: str | None) -> str:
        if self._codex.proved(path):
            return str(TranscriptProvenance.PROVEN)
        return super().correlation_for(path, session_id)

    def commit_attachment(self) -> None:
        super().commit_attachment()
        # One fact in two places: source's flag labels, observer's decides which key to ask.
        self._codex._session_exact = self._session_provenance is TranscriptProvenance.EXACT

    def _prepare_history_parse(self, fh: BinaryIO, start: int) -> None:
        self._codex._seed_history_context(fh, start)
