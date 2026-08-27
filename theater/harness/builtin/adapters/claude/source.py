"""Claude transcript source and history setup."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from theater.harness.source import Batch, ReceiptAdmission, TranscriptSource
from theater.harness.transcript.history import HistoryReader
from theater.provenance import TranscriptProvenance

if TYPE_CHECKING:
    from .observer import ClaudeCodeObserver


def _open_claude_source(
    *,
    root: Path,
    relocate_by_cwd: bool,
    cwd: str | None,
    session_id: str | None = None,
    after: float | None = None,
    session_provenance: str | TranscriptProvenance | None = None,
    known_location: str | None = None,
) -> _ClaudeSource:
    from .observer import ClaudeCodeObserver

    return _ClaudeSource(
        ClaudeCodeObserver(root=root),
        cwd=cwd,
        session_id=session_id,
        after=after,
        allow_refresh=relocate_by_cwd,
        session_provenance=session_provenance,
        known_location=known_location,
    )


class _ClaudeSource(TranscriptSource):
    """Keep a receipt pending until its JSONL materializes.

    A receipt path is not identity loss until the file has existed.
    """

    def __init__(self, observer: ClaudeCodeObserver, **kwargs) -> None:
        super().__init__(observer, **kwargs)
        self._claude = observer
        self._expected_location: Path | None = None

    def _history_reader(self) -> HistoryReader:
        from .observer import ClaudeCodeObserver

        reader = ClaudeCodeObserver(root=self._claude.root)
        return HistoryReader(
            parse_record=lambda line, index: reader.parse_record(line, index, clip_text=False),
            decorate_parsed=self._decorate_parsed,
            prepare_history_parse=reader._seed_mcp_context,
        )

    async def read(self) -> Batch:
        self._require_decision()
        path = self._expected_location
        if path is None:
            return await super().read()
        try:
            path.stat()
        except FileNotFoundError:
            relocated = await self._locate(session_id=self._session_id)
            if relocated is None or self._observer.session_id(relocated) != self._session_id:
                return Batch(waiting=True)
            path = relocated
        except OSError as exc:
            return self._source_unavailable_batch(exc)

        self._expected_location = None
        self._known_location = path
        self._known_location_provenance = TranscriptProvenance.EXACT
        self._proven[path] = TranscriptProvenance.EXACT
        return await super().read()

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        path = Path(location)
        self._pending = None
        self._session_id = session_id
        self._session_provenance = TranscriptProvenance.EXACT
        self._proven[path] = TranscriptProvenance.EXACT
        if self.path == path:
            self._expected_location = None
            self._known_location = path
            self._known_location_provenance = TranscriptProvenance.EXACT
            return "accepted"

        self._expected_location = path
        self._known_location = None
        self._known_location_provenance = TranscriptProvenance.HEURISTIC
        self._detach()
        return "staged"

    def revoke_attachment(self) -> None:
        self._expected_location = None
        super().revoke_attachment()
