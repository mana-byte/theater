"""Codex transcript discovery and live-process correlation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

from theater import proc
from theater.harness.source import Source, TranscriptCandidate
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
        return self._scan_by_cwd(cwd, after)

    def _by_session_id(self, session_id: str) -> Path | None:
        return next(self.root.glob(f"*/*/*/rollout-*-{session_id}.jsonl"), None)

    def proven_transcript(self, *, cwd: str | None) -> Path | None:
        """Probe proof without replacing an admitted heuristic location."""
        held = self._process_rollout(cwd)
        if held is not None:
            self._proved.add(held)
        return held

    def _scan_by_cwd(self, cwd: str, after: float | None) -> Path | None:
        want = str(Path(cwd).resolve()) if cwd else None
        if want is None:
            return None
        candidates = []
        for path in self.root.glob("*/*/*/rollout-*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            if after is not None:
                born = getattr(st, "st_birthtime", st.st_ctime)
                if born < after:
                    continue
            candidates.append((st.st_mtime, path))
        matches: list[Path] = []
        for _, path in sorted(candidates, reverse=True):
            if self._transcript_cwd(path) == want:
                matches.append(path)
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "codex find_transcript: %d transcripts match cwd %s; "
                "returning the newest — the observer will refuse a collision",
                len(matches),
                cwd,
            )
        return matches[0]

    def transcript_candidates(
        self,
        *,
        cwd: str | None,
        domain: str | None = None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        root = Path(domain).resolve() if domain else self.root.resolve()
        if not root.is_dir():
            return []
        want = str(Path(cwd).resolve()) if cwd else None
        resolved_domain = str(root)
        rows = [
            self._candidate_row(path, want=want, after=after, domain=resolved_domain)
            for path in root.glob("*/*/*/rollout-*.jsonl")
        ]
        return sorted(rows, key=lambda c: (c.mtime or 0, c.location), reverse=True)

    def identity_loss_candidate(
        self,
        *,
        cwd: str | None,
        current: Path,
        current_mtime_ns: int,
        after: float | None = None,
    ) -> Path | None:
        if not self.root.is_dir() or not cwd:
            return None
        want = str(Path(cwd).resolve())
        candidates: list[tuple[int, Path]] = []
        for path in self.root.glob("*/*/*/rollout-*.jsonl"):
            if path == current or path.is_symlink():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime_ns <= current_mtime_ns:
                continue
            if after is not None and getattr(st, "st_birthtime", st.st_ctime) < after:
                continue
            candidates.append((st.st_mtime_ns, path))
        for _mtime, path in sorted(candidates, reverse=True)[:_LOSS_CANDIDATE_PROBES]:
            if self._transcript_cwd(path) == want:
                return path
        return None

    def admit_operator_candidate(
        self,
        *,
        cwd: str | None,
        candidate: str,
        domain: str | None = None,
        after: float | None = None,
    ) -> TranscriptCandidate:
        want = str(Path(cwd).resolve()) if cwd else None
        root = Path(domain).resolve() if domain else self.root.resolve()
        path = Path(candidate).expanduser()
        if path.is_symlink():
            raise ValueError("candidate path is a symlink")
        real = path.resolve()
        if not real.is_relative_to(root):
            raise ValueError("candidate path is outside this harness transcript domain")
        row = self._candidate_row(real, want=want, after=after, domain=str(root))
        if row.rejection_reason:
            raise ValueError(row.rejection_reason)
        return row

    def _candidate_row(
        self,
        path: Path,
        *,
        want: str | None,
        after: float | None,
        domain: str,
    ) -> TranscriptCandidate:
        reason = None
        session_id = self.session_id(path)
        try:
            st = path.stat()
        except OSError:
            return TranscriptCandidate(
                location=str(path), rejection_reason="not readable", domain=domain
            )
        if after is not None and getattr(st, "st_birthtime", st.st_ctime) < after:
            reason = "created before participant floor"
        elif not self._is_rollout_shape(path, root=Path(domain)):
            reason = "harness shape mismatch"
        elif session_id is None:
            reason = "unextractable session id"
        else:
            found_cwd = self._transcript_cwd(path)
            if found_cwd is None:
                reason = "harness mismatch or unextractable cwd"
            elif want is not None and found_cwd != want:
                reason = "cwd mismatch"
        return TranscriptCandidate(
            location=str(path),
            session_id=session_id,
            mtime=st.st_mtime,
            size=st.st_size,
            rejection_reason=reason,
            domain=domain,
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
