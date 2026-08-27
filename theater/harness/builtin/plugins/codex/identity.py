"""Codex transcript discovery and live-process correlation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

from theater import proc
from theater.harness.contracts.callbacks import (
    OperatorCandidateContext,
    TranscriptCandidatesContext,
)
from theater.harness.source import Source, TranscriptCandidate
from theater.harness.transcript.discovery import GlobDiscovery, stat_birthtime
from theater.provenance import TranscriptProvenance, normalize_provenance

from .constants import (
    _CWD_PROBE_BYTES,
    _LOSS_CANDIDATE_PROBES,
    _STEM,
    CODEX_BINARY,
    CODEX_SESSION_META_RECORD_TYPE,
)
from .source import _open_codex_source

logger = logging.getLogger("theater.harness.codex")


def transcript_candidates(
    context: TranscriptCandidatesContext, *, root: Path | None = None
) -> list[TranscriptCandidate]:
    from .observer import CodexObserver

    return CodexObserver(root=root).transcript_candidates(
        cwd=context.cwd,
        domain=context.domain,
        after=context.after,
    )


def admit_operator_candidate(
    context: OperatorCandidateContext, *, root: Path | None = None
) -> TranscriptCandidate:
    from .observer import CodexObserver

    return CodexObserver(root=root).admit_operator_candidate(
        cwd=context.cwd,
        candidate=context.candidate,
        domain=context.domain,
        after=context.after,
    )


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _is_codex(comm: str) -> bool:
    return comm.rsplit("/", 1)[-1] == CODEX_BINARY


class CodexIdentityMixin:
    if TYPE_CHECKING:
        root: Path
        pane_pid: int | None
        _proved: set[Path]
        _session_exact: bool

    @property
    def _discovery(self) -> GlobDiscovery:
        return GlobDiscovery(
            root=self.root,
            glob_pattern="*/*/*/rollout-*.jsonl",
            session_id_of=self.session_id,
            cwd_of=self._transcript_cwd,
            is_shape=self._is_rollout_shape,
            birthtime_of=stat_birthtime,
            loss_probes=_LOSS_CANDIDATE_PROBES,
            collision_warning=(
                "codex find_transcript: %d transcripts match cwd %s; "
                "returning the newest — the observer will refuse a collision"
            ),
        )

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
    ) -> Source:
        from .observer import CodexObserver

        provenance = normalize_provenance(session_provenance)
        session_exact = provenance is TranscriptProvenance.EXACT
        reader = cast(CodexObserver, self)
        if session_exact != self._session_exact:
            reader = CodexObserver(
                root=self.root, pane_pid=self.pane_pid, session_exact=session_exact
            )
        return _open_codex_source(
            reader,
            cwd=cwd,
            session_id=session_id,
            after=after,
            session_provenance=provenance,
            known_location=known_location,
        )

    def open_source_for(
        self,
        *,
        participant_id: str,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
        pane_pid: int | None = None,
    ) -> Source:
        from .observer import CodexObserver

        provenance = normalize_provenance(session_provenance)
        session_exact = provenance is TranscriptProvenance.EXACT
        reader = CodexObserver(root=self.root, pane_pid=pane_pid, session_exact=session_exact)
        return _open_codex_source(
            reader,
            cwd=cwd,
            session_id=session_id,
            after=after,
            session_provenance=provenance,
            known_location=known_location,
        )

    def proved(self, path: Path) -> bool:
        return _resolve(path) in self._proved

    def find_transcript(
        self,
        *,
        cwd: str,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        if not self.root.is_dir():
            return None
        if session_id and self._session_exact:
            hit = self._by_session_id(session_id)
            if hit is not None:
                return hit
        held = self.proven_transcript(cwd=cwd)
        if held is not None:
            return held
        if session_id:
            hit = self._by_session_id(session_id)
            if hit is not None:
                return hit
        return self._discovery.find_transcript(cwd=cwd, after=after)

    def _by_session_id(self, session_id: str) -> Path | None:
        return next(self.root.glob(f"*/*/*/rollout-*-{session_id}.jsonl"), None)

    def proven_transcript(self, *, cwd: str | None) -> Path | None:
        """Probe proof without replacing an admitted heuristic location."""
        held = self._process_rollout(cwd)
        if held is not None:
            self._proved.add(held)
        return held

    def transcript_candidates(
        self,
        *,
        cwd: str | None,
        domain: str | None = None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        return self._discovery.transcript_candidates(cwd=cwd, domain=domain, after=after)

    def identity_loss_candidate(
        self,
        *,
        cwd: str | None,
        current: Path,
        current_mtime_ns: int,
        after: float | None = None,
    ) -> Path | None:
        return self._discovery.identity_loss_candidate(
            cwd=cwd, current=current, current_mtime_ns=current_mtime_ns, after=after
        )

    def admit_operator_candidate(
        self,
        *,
        cwd: str | None,
        candidate: str,
        domain: str | None = None,
        after: float | None = None,
    ) -> TranscriptCandidate:
        return self._discovery.admit_operator_candidate(
            cwd=cwd, candidate=candidate, domain=domain, after=after
        )

    def _is_rollout_shape(self, path: Path, *, root: Path | None = None) -> bool:
        if path.suffix != ".jsonl" or _STEM.match(path.stem) is None:
            return False
        try:
            relative = path.resolve().relative_to((root or self.root).resolve())
        except (OSError, ValueError):
            return False
        return len(relative.parts) == 4

    def _process_rollout(self, cwd: str | None) -> Path | None:
        """Accept exactly one rollout held by this participant's process."""
        pid = self._owning_process()
        if pid is None:
            return None
        want = _resolve(Path(cwd)) if cwd else None
        root = _resolve(self.root)
        found: set[Path] = set()
        for path in proc.open_files(pid):
            if not self._is_rollout(path, root):
                continue
            if want is not None and self._transcript_cwd(path) != str(want):
                continue
            found.add(_resolve(path))
        if not found:
            return None
        if len(found) > 1:
            logger.warning(
                "codex process %s holds %d rollouts open under %s; "
                "declining to pick one — falling back to cwd discovery",
                pid,
                len(found),
                self.root,
            )
            return None
        return found.pop()

    def _owning_process(self) -> int | None:
        """Only the recorded pane process can prove a rollout owner.

        Descendants and adopted shells fail closed: they do not identify the original session.
        """
        if self.pane_pid is None:
            return None
        if not _is_codex(proc.comm(self.pane_pid)):
            return None
        return self.pane_pid

    def _is_rollout(self, path: Path, root: Path) -> bool:
        if path.suffix != ".jsonl" or _STEM.match(path.stem) is None:
            return False
        return _resolve(path).is_relative_to(root)

    def _transcript_cwd(self, path: Path) -> str | None:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                line = fh.readline(_CWD_PROBE_BYTES)
        except OSError:
            return None
        try:
            record = json.loads(line)
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get("type") != CODEX_SESSION_META_RECORD_TYPE:
            return None
        payload = record.get("payload")
        found = payload.get("cwd") if isinstance(payload, dict) else None
        return str(Path(found).resolve()) if found else None

    def session_id(self, transcript: Path) -> str | None:
        found = _STEM.match(transcript.stem)
        return found.group(1) if found else None
