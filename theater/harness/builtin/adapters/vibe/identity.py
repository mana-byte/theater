"""Vibe transcript domains, discovery, and operator admission."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from theater import paths
from theater.harness.source import TranscriptCandidate
from theater.harness.transcript.discovery import GlobDiscovery, parent_birthtime

from .constants import _SCAN_LIMIT, MESSAGES_FILENAME, META_FILENAME, SESSION_DIRECTORY_PREFIX
from .isolation import _canonical

logger = logging.getLogger("theater.harness.vibe")


class VibeIdentityMixin:
    root: Path
    correlation_root: Path | None
    isolated: bool
    _cwd: str | None

    @property
    def _discovery(self) -> GlobDiscovery:
        return GlobDiscovery(
            root=self.root,
            glob_pattern=f"{SESSION_DIRECTORY_PREFIX}*/{MESSAGES_FILENAME}",
            session_id_of=self.session_id,
            cwd_of=self._cwd_of,
            is_shape=self._is_vibe_shape,
            birthtime_of=parent_birthtime,
            loss_probes=0,
            collision_warning=(
                "vibe find_transcript: %d session directories match cwd %s; "
                "returning a heuristic candidate for the reducer to validate"
            ),
        )

    def _cwd_of(self, path: Path) -> str | None:
        return self._meta_cwd(path.parent)

    @staticmethod
    def _is_vibe_shape(path: Path, *, root: Path) -> bool:
        return path.name == MESSAGES_FILENAME and path.parent.name.startswith(
            SESSION_DIRECTORY_PREFIX
        )

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
            self._discovery._candidate_row(
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
        return self._discovery.admit_operator_candidate(
            cwd=cwd, candidate=candidate, domain=domain, after=after
        )

    def _is_candidate(self, d: Path, want: str, after: float | None) -> bool:
        """Check transcript shape, creation floor, and Vibe cwd."""
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
