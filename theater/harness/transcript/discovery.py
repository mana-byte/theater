"""Generic transcript-discovery strategies shared across adapters.

No harness name, native filename, or glob literal appears here. Each adapter
supplies pluggable seams: the glob pattern, session-id extractor, cwd extractor,
shape validator, and birthtime source.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol

from theater.harness.contracts.source import TranscriptCandidate

if TYPE_CHECKING:
    from theater.harness.contracts.launch import ResumeLaunchOverlay
    from theater.harness.contracts.trajectory import ParsedRecord
    from theater.harness.transcript.history import HistoryReader


class _HistoryClone(Protocol):
    def parse_record(self, line: str, index: int, *, clip_text: bool) -> ParsedRecord: ...


class _Predecessor(Protocol):
    transcript_domain: str | None


logger = logging.getLogger("theater.harness.source")


class CwdOf(Protocol):
    def __call__(self, path: Path) -> str | None: ...


class SessionIdOf(Protocol):
    def __call__(self, path: Path) -> str | None: ...


class IsShape(Protocol):
    def __call__(self, path: Path, *, root: Path) -> bool: ...


class BirthtimeOf(Protocol):
    def __call__(self, path: Path, st: os.stat_result) -> float: ...


def stat_birthtime(path: Path, st: os.stat_result) -> float:
    return getattr(st, "st_birthtime", st.st_ctime)


def parent_birthtime(path: Path, st: os.stat_result) -> float:
    try:
        parent_st = path.parent.stat()
    except OSError:
        return st.st_ctime
    return getattr(parent_st, "st_birthtime", parent_st.st_ctime)


@dataclass(frozen=True, slots=True)
class GlobDiscovery:
    """Pluggable glob-based transcript discovery and operator admission."""

    root: Path
    glob_pattern: str
    session_id_of: Callable[[Path], str | None]
    cwd_of: Callable[[Path], str | None]
    is_shape: IsShape
    birthtime_of: Callable[[Path, os.stat_result], float]
    loss_probes: int
    collision_warning: str

    def find_transcript(
        self,
        *,
        cwd: str,
        after: float | None = None,
    ) -> Path | None:
        if not self.root.is_dir():
            return None
        want = str(Path(cwd).resolve()) if cwd else None
        if want is None:
            return None
        candidates: list[tuple[float, Path]] = []
        for path in self.root.glob(self.glob_pattern):
            try:
                st = path.stat()
            except OSError:
                continue
            if after is not None:
                born = self.birthtime_of(path, st)
                if born < after:
                    continue
            candidates.append((st.st_mtime, path))
        matches: list[Path] = []
        for _, path in sorted(candidates, reverse=True):
            if self.cwd_of(path) == want:
                matches.append(path)
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(self.collision_warning, len(matches), cwd)
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
            for path in root.glob(self.glob_pattern)
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
        for path in self.root.glob(self.glob_pattern):
            if path == current or path.is_symlink():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime_ns <= current_mtime_ns:
                continue
            if after is not None and self.birthtime_of(path, st) < after:
                continue
            candidates.append((st.st_mtime_ns, path))
        for _mtime, path in sorted(candidates, reverse=True)[: self.loss_probes]:
            if self.cwd_of(path) == want:
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
        session_id = self.session_id_of(path)
        try:
            st = path.stat()
        except OSError:
            return TranscriptCandidate(
                location=str(path),
                session_id=session_id,
                rejection_reason="not readable",
                domain=domain,
            )
        if after is not None and self.birthtime_of(path, st) < after:
            reason = "created before participant floor"
        elif not self.is_shape(path, root=Path(domain)):
            reason = "harness shape mismatch"
        elif session_id is None:
            reason = "unextractable session id"
        else:
            found_cwd = self.cwd_of(path)
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


def stateful_history_reader(
    clone: Callable[[], _HistoryClone],
    seed_of: Callable[[_HistoryClone], Callable[[BinaryIO, int], None]],
    decorate: Callable[[ParsedRecord, int], ParsedRecord],
) -> HistoryReader:
    """Build a HistoryReader from a fresh cloned observer.

    ``clone`` returns a new observer instance; ``seed_of`` extracts the
    bound method used for ``prepare_history_parse``.
    """
    from theater.harness.transcript.history import HistoryReader

    reader = clone()
    return HistoryReader(
        parse_record=lambda line, index: reader.parse_record(line, index, clip_text=False),
        decorate_parsed=decorate,
        prepare_history_parse=seed_of(reader),
    )


def root_domain_overlay(
    predecessor: _Predecessor,
    expected: str,
    label: str,
    *,
    resolve_declared: bool = False,
    noun: str = "domain",
) -> ResumeLaunchOverlay:
    """Validate that a predecessor's transcript domain matches the expected root."""
    from theater.harness.contracts.launch import ResumeLaunchOverlay
    from theater.models import BadRequest

    if predecessor.transcript_domain is None:
        return ResumeLaunchOverlay()
    declared = predecessor.transcript_domain
    if resolve_declared:
        declared = str(Path(declared).resolve(strict=False))
    if declared != expected:
        raise BadRequest(
            f"cannot resume {label} session: predecessor transcript domain "
            f"{declared!r} does not match the {label} "
            f"observation {noun} {expected!r}"
        )
    return ResumeLaunchOverlay(transcript_domain=expected)


def screen_tail(capture: str, n: int, *, skip_blank: bool = True) -> list[str]:
    """Return the last ``n`` lines from a screen capture."""
    if n <= 0:
        return []
    lines = capture.splitlines()
    if skip_blank:
        lines = [line for line in lines if line.strip()]
    return lines[-n:]
