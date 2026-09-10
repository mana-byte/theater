"""Vibe transcript domains, discovery, and operator admission."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from theater import paths
from theater.harness.source import TranscriptCandidate
from theater.harness.transcript.discovery import GlobDiscovery, parent_birthtime
from theater.provenance import TranscriptProvenance

from .constants import _SCAN_LIMIT, MESSAGES_FILENAME, META_FILENAME, SESSION_DIRECTORY_PREFIX
from .isolation import _canonical
from .unified_store import UnifiedStoreError, UnifiedStoreView, load_unified_store

logger = logging.getLogger("theater.harness.vibe")
_UNIFIED_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def participant_root(participant_id: str, correlation_root: Path | None = None) -> Path:
    if correlation_root is not None:
        return correlation_root / participant_id
    return paths.participant_observation_dir(participant_id, "vibe")


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
        return participant_root(participant_id, self.correlation_root)

    def _root_searchable(self) -> bool:
        try:
            st = self.root.lstat()
        except OSError:
            return False
        return self.root.is_dir() and not self.root.is_symlink() and st.st_uid == os.geteuid()

    def find_legacy_transcript(
        self,
        *,
        cwd: str | None,
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

    def _unified_view(self, current: Path) -> UnifiedStoreView | None:
        try:
            return load_unified_store(current)
        except (OSError, ValueError, UnifiedStoreError):
            return None

    @staticmethod
    def _unified_metadata(view: UnifiedStoreView) -> tuple[str | None, str | None, float, float]:
        metadata = view.runtime_state.get("session_metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        cwd = metadata.get("cwd")
        parent = metadata.get("parent_session_id")
        session = view.snapshot.get("session")
        session = session if isinstance(session, dict) else {}
        created = session.get("createdAt")
        updated = session.get("updatedAt")
        return (
            str(Path(cwd).resolve()) if isinstance(cwd, str) and cwd else None,
            parent if isinstance(parent, str) and parent else None,
            created / 1000 if isinstance(created, int) and not isinstance(created, bool) else 0.0,
            updated / 1000 if isinstance(updated, int) and not isinstance(updated, bool) else 0.0,
        )

    def find_unified_transcript(  # noqa: PLR0912
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        """Find a root Unified store; child stores are lineage, not primary sessions."""
        if not self._root_searchable():
            return None
        unified = self.root / "unified"
        if not unified.is_dir() or unified.is_symlink():
            return None
        if session_id:
            if _UNIFIED_SESSION_ID.fullmatch(session_id) is None:
                return None
            exact = unified / session_id / "CURRENT"
            if exact.is_file():
                return exact
            if len(session_id) <= 8:
                prefix_matches = sorted(
                    path / "CURRENT"
                    for path in unified.iterdir()
                    if path.is_dir()
                    and path.name.startswith(session_id)
                    and (path / "CURRENT").is_file()
                )
                if len(prefix_matches) == 1:
                    return prefix_matches[0]
            return None
        want = str(Path(cwd).resolve()) if cwd else None
        if want is None:
            return None
        candidates: list[tuple[float, Path]] = []
        seen = 0
        try:
            paths = sorted(
                unified.glob("*/CURRENT"), key=lambda path: path.stat().st_mtime, reverse=True
            )
        except OSError:
            return None
        for current in paths:
            seen += 1
            if seen > _SCAN_LIMIT:
                break
            view = self._unified_view(current)
            if view is None:
                continue
            found_cwd, parent, created, updated = self._unified_metadata(view)
            if parent is not None or found_cwd != want or (after is not None and created < after):
                continue
            candidates.append((updated, current))
        candidates.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
        return candidates[0][1] if candidates else None

    async def find_unified_transcript_async(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        return await asyncio.to_thread(
            self.find_unified_transcript, cwd=cwd, session_id=session_id, after=after
        )

    def find_transcript(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        """Compatibility discovery across legacy JSONL and Unified stores."""
        unified = self.find_unified_transcript(cwd=cwd, session_id=session_id, after=after)
        legacy = self.find_legacy_transcript(cwd=cwd, session_id=session_id, after=after)
        if unified is None:
            return legacy
        if legacy is None or session_id is not None:
            return unified
        view = self._unified_view(unified)
        _cwd, _parent, _created, unified_updated = (
            self._unified_metadata(view) if view is not None else (None, None, 0.0, 0.0)
        )
        try:
            legacy_updated = legacy.stat().st_mtime
        except OSError:
            legacy_updated = 0.0
        return unified if unified_updated >= legacy_updated else legacy

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
            self._discovery.candidate_row(
                d / MESSAGES_FILENAME,
                want=want,
                after=after,
                domain=resolved_domain,
            )
            for d in root.glob(f"{SESSION_DIRECTORY_PREFIX}*")
        ]
        unified = root / "unified"
        if unified.is_dir() and not unified.is_symlink():
            seen = 0
            for current in unified.glob("*/CURRENT"):
                seen += 1
                if seen > _SCAN_LIMIT:
                    break
                view = self._unified_view(current)
                if view is None:
                    continue
                found_cwd, parent, _created, updated = self._unified_metadata(view)
                rejection = None
                if parent is not None:
                    rejection = (
                        "Unified child sessions are lineage records, not primary transcripts"
                    )
                elif want is not None and found_cwd != want:
                    rejection = f"working directory is {found_cwd!r}, expected {want!r}"
                try:
                    size = current.stat().st_size
                except OSError:
                    size = None
                rows.append(
                    TranscriptCandidate(
                        location=str(current.resolve()),
                        session_id=view.session_id,
                        mtime=updated,
                        size=size,
                        provenance=str(TranscriptProvenance.HEURISTIC),
                        rejection_reason=rejection,
                        domain=resolved_domain,
                    )
                )
        # A Unified import and its legacy origin may share an id; expose only the active form.
        unified_ids = {
            row.session_id
            for row in rows
            if Path(row.location).name == "CURRENT"
            and Path(row.location).parent.parent.name == "unified"
        }
        rows = [
            row
            for row in rows
            if (
                Path(row.location).name == "CURRENT"
                and Path(row.location).parent.parent.name == "unified"
            )
            or row.session_id not in unified_ids
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
        path = Path(candidate).expanduser().resolve()
        if path.name == "CURRENT" and path.parent.parent.name == "unified":
            root = _canonical(Path(domain)) if domain else self.root.resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError("Unified Vibe candidate is outside the transcript domain") from exc
            view = load_unified_store(path)
            if view is None:
                raise ValueError("Unified Vibe candidate is unavailable")
            found_cwd, parent, _created, updated = self._unified_metadata(view)
            want = str(Path(cwd).resolve()) if cwd else None
            rejection = None
            if parent is not None:
                rejection = "Unified child sessions cannot be bound as primary transcripts"
            elif want is not None and found_cwd != want:
                rejection = f"working directory is {found_cwd!r}, expected {want!r}"
            return TranscriptCandidate(
                location=str(path),
                session_id=view.session_id,
                mtime=updated,
                size=path.stat().st_size,
                provenance=str(TranscriptProvenance.OPERATOR),
                rejection_reason=rejection,
                domain=str(root),
            )
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
        if transcript.name == "CURRENT" and transcript.parent.parent.name == "unified":
            return transcript.parent.name
        found = self._meta(transcript.parent).get("session_id")
        if found:
            return str(found)
        name = transcript.parent.name
        return name.rsplit("_", 1)[-1] if name.startswith(SESSION_DIRECTORY_PREFIX) else None
