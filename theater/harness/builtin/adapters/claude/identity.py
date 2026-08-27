"""Claude transcript discovery and lifecycle receipt validation.

The lossy directory slug is never reconstructed; records provide the trusted cwd.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

from theater.harness.source import TranscriptCandidate
from theater.provenance import TranscriptProvenance

from .constants import _CWD_PROBE_BYTES, _CWD_PROBE_RECORDS, _LOSS_CANDIDATE_PROBES
from .launch import _hook_string
from .source import _ClaudeSource

logger = logging.getLogger("theater.harness.claude")


class ClaudeIdentity:
    """Transcript layout is ``~/.claude/projects/<slugged-cwd>/<session>.jsonl``."""

    root: Path
    relocate_by_cwd: bool

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> _ClaudeSource:
        from .observer import ClaudeCodeObserver

        reader = ClaudeCodeObserver(root=self.root)
        return _ClaudeSource(
            reader,
            cwd=cwd,
            session_id=session_id,
            after=after,
            allow_refresh=self.relocate_by_cwd,
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
    ) -> _ClaudeSource:
        from .observer import ClaudeCodeObserver

        reader = ClaudeCodeObserver(root=self.root)
        return _ClaudeSource(
            reader,
            cwd=cwd,
            session_id=session_id,
            after=after,
            allow_refresh=self.relocate_by_cwd,
            session_provenance=session_provenance,
            known_location=known_location,
        )

    def find_transcript(
        self,
        *,
        cwd: str,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        if not self.root.is_dir():
            return None
        if session_id:
            hit = next(self.root.glob(f"*/{session_id}.jsonl"), None)
            if hit is not None:
                return hit
        want = str(Path(cwd).resolve()) if cwd else None
        if want is None:
            return None
        candidates = []
        for path in self.root.glob("*/*.jsonl"):
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
                "claude find_transcript: %d transcripts match cwd %s; "
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
        rows: list[TranscriptCandidate] = []
        resolved_domain = str(root)
        for path in root.glob("*/*.jsonl"):
            rows.append(self._candidate_row(path, want=want, after=after, domain=resolved_domain))
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
        for path in self.root.glob("*/*.jsonl"):
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
        session_id = path.stem if path.suffix == ".jsonl" and path.stem else None
        try:
            st = path.stat()
        except OSError:
            return TranscriptCandidate(
                location=str(path), rejection_reason="not readable", domain=domain
            )
        if after is not None and getattr(st, "st_birthtime", st.st_ctime) < after:
            reason = "created before participant floor"
        elif path.suffix != ".jsonl" or path.parent.parent.resolve() != Path(domain).resolve():
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

    def _transcript_cwd(self, path: Path) -> str | None:
        read = 0
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    read += len(line)
                    if i >= _CWD_PROBE_RECORDS or read > _CWD_PROBE_BYTES:
                        return None
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    found = isinstance(record, dict) and record.get("cwd")
                    if found:
                        return str(Path(found).resolve())
        except OSError:
            return None
        return None

    def session_id(self, transcript: Path) -> str | None:
        return transcript.stem or None

    def validate_transcript_receipt(
        self,
        *,
        payload: Mapping[str, object],
        cwd: str | None,
        expected_session_id: str | None,
    ) -> TranscriptCandidate:
        session_id = _hook_string(payload, "session_id", "sessionId")
        transcript_path = _hook_string(payload, "transcript_path", "transcriptPath")
        if session_id is None:
            raise ValueError(
                "claude receipt payload is missing session_id (or sessionId); "
                "the lifecycle hook must provide it"
            )
        if transcript_path is None:
            raise ValueError(
                "claude receipt payload is missing transcript_path "
                "(or transcriptPath); the lifecycle hook must provide it"
            )
        if not isinstance(self.root, Path):
            raise ValueError(  # noqa: TRY004 – rejection is always ValueError per design
                "claude receipt cannot validate transcript root: observer root is not a Path"
            )
        root = self.root.resolve()
        path = Path(transcript_path).expanduser()
        if not path.is_absolute():
            raise ValueError(
                f"claude receipt transcript_path must be absolute, got {transcript_path!r}"
            )
        canonical = path.resolve(strict=False)
        try:
            rel = canonical.relative_to(root)
        except ValueError:
            raise ValueError(
                f"claude receipt transcript_path {transcript_path!r} is outside "
                "Claude's transcript root"
            ) from None
        if canonical.suffix != ".jsonl" or len(rel.parts) != 2:
            raise ValueError(
                "claude receipt transcript_path must name a Claude project "
                "JSONL transcript (<project>/<session>.jsonl)"
            )
        if canonical.stem != session_id:
            raise ValueError(
                f"claude receipt session_id {session_id!r} does not match "
                f"transcript_path filename stem {canonical.stem!r}"
            )
        found_evidence = self._validate_transcript_records(
            canonical, session_id=session_id, cwd=cwd
        )
        if not found_evidence and session_id != expected_session_id:
            raise ValueError(
                "claude receipt transcript does not yet contain matching "
                "evidence (no record carries session_id and the id does not "
                "match the launch session)"
            )
        return TranscriptCandidate(location=str(canonical), session_id=session_id)

    @staticmethod
    def _validate_transcript_records(path: Path, *, session_id: str, cwd: str | None) -> bool:
        if not path.exists():
            return False
        wanted_cwd = str(Path(cwd).resolve()) if cwd else None
        found_evidence = False
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i >= 20:
                        return found_evidence
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    found_session = record.get("session_id") or record.get("sessionId")
                    if (
                        isinstance(found_session, str)
                        and found_session
                        and found_session != session_id
                    ):
                        raise ValueError("claude receipt session_id contradicts transcript records")
                    if found_session == session_id:
                        found_evidence = True
                    found_cwd = record.get("cwd")
                    if (
                        wanted_cwd is not None
                        and isinstance(found_cwd, str)
                        and found_cwd
                        and str(Path(found_cwd).resolve()) != wanted_cwd
                    ):
                        raise ValueError(
                            "claude receipt transcript cwd contradicts participant cwd"
                        )
                    if isinstance(found_cwd, str) and found_cwd:
                        found_evidence = True
        except OSError as exc:
            raise ValueError(f"claude receipt transcript_path is not readable: {exc}") from exc
        return found_evidence
