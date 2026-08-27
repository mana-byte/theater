"""Vibe transcript domains, discovery, and operator admission."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from theater import paths
from theater.harness.source import TranscriptCandidate

from .constants import _SCAN_LIMIT, MESSAGES_FILENAME, META_FILENAME, SESSION_DIRECTORY_PREFIX
from .isolation import _canonical

logger = logging.getLogger("theater.harness.vibe")


class VibeIdentityMixin:
    root: Path
    correlation_root: Path | None
    isolated: bool
    _cwd: str | None

    def participant_root(self, participant_id: str) -> Path:
        base = self.correlation_root or paths.home() / "observations" / "vibe"
        return base / participant_id

    def _root_searchable(self) -> bool:
        try:
            st = self.root.lstat()
        except OSError:
            return False
        return self.root.is_dir() and not self.root.is_symlink() and st.st_uid == os.geteuid()

    def find_transcript(
        self,
        *,
        cwd: str,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        self._cwd = cwd
        if not self._root_searchable():
            return None
        if session_id:
            short = session_id.split("-")[0][:8]
            for d in sorted(self.root.glob(f"{SESSION_DIRECTORY_PREFIX}*_{short}"), reverse=True):
                messages = d / MESSAGES_FILENAME
                if messages.exists():
                    return messages
        want = str(Path(cwd).resolve()) if cwd else None
        if want is None:
            return None
        # Fixed-width UTC timestamp in dir names; reverse lexicographic = newest. Siblings match.
        matches: list[Path] = []
        seen = 0
        for d in sorted(self.root.glob(f"{SESSION_DIRECTORY_PREFIX}*"), reverse=True):
            seen += 1
            if seen > _SCAN_LIMIT:
                break
            if not self._is_candidate(d, want, after):
                continue
            matches.append(d / MESSAGES_FILENAME)
        if not matches:
            return None
        if len(matches) > 1 and not self.isolated:
            logger.warning(
                "vibe find_transcript: %d session directories match cwd %s; "
                "returning a heuristic candidate for the reducer to validate",
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
        root = _canonical(Path(domain)) if domain else self.root.resolve()
        if not root.is_dir():
            return []
        want = str(Path(cwd).resolve()) if cwd else None
        resolved_domain = str(root)
        rows = [
            self._candidate_row(
                d / MESSAGES_FILENAME,
                want=want,
                after=after,
                domain=resolved_domain,
            )
            for d in root.glob(f"{SESSION_DIRECTORY_PREFIX}*")
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
        """Reuse Vibe's already-bounded newest-first session search."""
        if not cwd:
            return None
        candidate = self.find_transcript(cwd=cwd, session_id=None, after=after)
        if candidate is None or candidate == current:
            return None
        try:
            return candidate if candidate.stat().st_mtime_ns > current_mtime_ns else None
        except OSError:
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
                location=str(path),
                session_id=session_id,
                rejection_reason="not readable",
                domain=domain,
            )
        if after is not None:
            try:
                born = getattr(path.parent.stat(), "st_birthtime", path.parent.stat().st_ctime)
            except OSError:
                born = st.st_ctime
            if born < after:
                reason = "created before participant floor"
        if reason is None and (
            path.name != MESSAGES_FILENAME
            or not path.parent.name.startswith(SESSION_DIRECTORY_PREFIX)
        ):
            reason = "harness shape mismatch"
        elif reason is None and session_id is None:
            reason = "unextractable session id"
        elif reason is None:
            found_cwd = self._meta_cwd(path.parent)
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

    def _is_candidate(self, d: Path, want: str, after: float | None) -> bool:
        """Whether a session directory is a viable transcript match.

        Checks the messages file exists, the birth-time floor, and the cwd
        from meta.json — the three conditions that were inline branches in
        ``find_transcript`` before they overflowed the branch limit.
        """
        messages = d / MESSAGES_FILENAME
        if not messages.exists():
            return False
        if after is not None:
            try:
                st = d.stat()
            except OSError:
                return False
            # Stat, not the name: its timestamp has no timezone, and the caller's floor is epoch.
            if getattr(st, "st_birthtime", st.st_ctime) < after:
                return False
        return self._meta_cwd(d) == want

    def _meta(self, session_dir: Path) -> dict:
        try:
            data = json.loads((session_dir / META_FILENAME).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _meta_cwd(self, session_dir: Path) -> str | None:
        env = self._meta(session_dir).get("environment") or {}
        found = env.get("working_directory") if isinstance(env, dict) else None
        return str(Path(found).resolve()) if found else None

    def session_id(self, transcript: Path) -> str | None:
        """meta.json is authoritative; the directory suffix is only 8 chars."""
        found = self._meta(transcript.parent).get("session_id")
        if found:
            return str(found)
        name = transcript.parent.name
        return name.rsplit("_", 1)[-1] if name.startswith(SESSION_DIRECTORY_PREFIX) else None
