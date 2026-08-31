"""Pi transcript discovery and source wiring."""

from __future__ import annotations

import json
import os
from pathlib import Path

from theater.harness.contracts.callbacks import (
    OperatorCandidateContext,
    ScreenContext,
    TranscriptCandidatesContext,
)
from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.source import Source, TranscriptCandidate
from theater.harness.observation import ScreenKind, ScreenReading, TranscriptObserver
from theater.provenance import TranscriptProvenance
from theater.trajectory.capabilities import TrajectoryCapabilities, TrajectoryFeature

from .constants import PI_HEADER_BYTES, PI_SESSIONS_DIRNAME
from .isolation import canonical, validate_domain
from .launch import participant_root
from .parser import PiParserMixin
from .screen import classify_screen
from .source import PiTranscriptSource


def _default_sessions_root(cwd: str | None) -> Path:
    resolved = str(Path(cwd or ".").expanduser().resolve())
    encoded_path = resolved.lstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-")
    encoded = f"--{encoded_path}--"
    return Path.home() / ".pi" / "agent" / PI_SESSIONS_DIRNAME / encoded


def _header(path: Path) -> dict | None:
    """Read only Pi's first JSONL record; never parse an unbounded header."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            raw = stream.readline(PI_HEADER_BYTES + 1)
    except OSError:
        return None
    if len(raw) > PI_HEADER_BYTES or not raw.endswith(b"\n"):
        return None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("type") != "session":
        return None
    if not isinstance(value.get("id"), str) or not value["id"]:
        return None
    if not isinstance(value.get("cwd"), str) or not value["cwd"]:
        return None
    return value


def _header_cwd(value: dict) -> str:
    return str(Path(value["cwd"]).expanduser().resolve())


class PiObserver(PiParserMixin, TranscriptObserver):
    """Observe Pi JSONL sessions, including deliberate `/new` rotations."""

    relocate_by_cwd = True
    trajectory_capabilities = TrajectoryCapabilities(
        supported=frozenset(
            {
                TrajectoryFeature.MODELS,
                TrajectoryFeature.TOOLS,
                TrajectoryFeature.USAGE,
                TrajectoryFeature.TIMING,
                TrajectoryFeature.REASONING,
                TrajectoryFeature.CONTEXT,
                TrajectoryFeature.LIVE_UPDATES,
            }
        ),
        unsupported=frozenset({TrajectoryFeature.REQUESTS, TrajectoryFeature.RETRIES}),
    )

    def __init__(self, root: Path | None = None, *, isolated: bool = False) -> None:
        self.root = root
        self.isolated = isolated
        self._reset_turn_context()

    def _source_root(self, cwd: str | None) -> Path:
        return canonical(self.root) if self.root is not None else _default_sessions_root(cwd)

    def screen_reading(self, capture: str) -> ScreenReading:
        return classify_screen(ScreenContext(capture=capture))

    def is_idle_screen(self, capture: str) -> bool:
        return self.screen_reading(capture).kind is ScreenKind.PROMPT

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
        usage_floor: str | None = None,
    ) -> Source:
        root = self._source_root(cwd)
        reader = self if root == self.root else PiObserver(root=root, isolated=self.isolated)
        return PiTranscriptSource(
            reader,
            cwd=cwd,
            session_id=session_id,
            after=after,
            allow_refresh=True,
            exact_attachments=reader.isolated,
            session_provenance=session_provenance,
            collision_domain=str(root),
            known_location=known_location,
            usage_floor=usage_floor,
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
        transcript_domain: str | None = None,
        usage_floor: str | None = None,
    ) -> Source:
        domain = (
            canonical(Path(transcript_domain))
            if transcript_domain
            else participant_root(participant_id)
        )
        marker = validate_domain(domain, participant_id=participant_id)
        # A resumed participant intentionally reads its predecessor's signed
        # isolated domain.  Only its durable accounting boundary authorizes
        # that owner mismatch; cold participants still require their own
        # marker so a signed sibling domain cannot be adopted by accident.
        if marker is not None or (usage_floor is not None and validate_domain(domain) is not None):
            return PiObserver(root=domain, isolated=True).open_source(
                cwd=cwd,
                session_id=session_id,
                after=after,
                session_provenance=session_provenance,
                known_location=known_location,
                usage_floor=usage_floor,
            )
        return self.open_source(
            cwd=cwd,
            session_id=session_id,
            after=after,
            session_provenance=session_provenance,
            known_location=known_location,
            usage_floor=usage_floor,
        )

    def open_source_context(self, context: ParticipantObservationContext) -> Source:
        return self.open_source_for(
            participant_id=context.participant_id,
            cwd=context.cwd,
            session_id=context.session_id,
            after=context.after,
            session_provenance=context.session_provenance,
            known_location=context.known_location,
            transcript_domain=context.transcript_domain,
            usage_floor=context.usage_floor,
        )

    def find_transcript(
        self,
        *,
        cwd: str,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        root = self._source_root(cwd)
        if not _safe_directory(root):
            return None
        wanted_cwd = str(Path(cwd).expanduser().resolve())
        candidates: list[tuple[int, Path]] = []
        for path in root.glob("*.jsonl"):
            if path.is_symlink():
                continue
            header = _header(path)
            if header is None:
                continue
            found_id = header["id"]
            if session_id is not None:
                if found_id == session_id:
                    return canonical(path)
                continue
            if _header_cwd(header) != wanted_cwd:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if after is not None and stat.st_ctime < after:
                continue
            candidates.append((stat.st_mtime_ns, canonical(path)))
        return max(candidates, default=(0, None))[1]

    def session_id(self, transcript: Path) -> str | None:
        header = _header(transcript)
        return header["id"] if header is not None else None

    def transcript_candidates(
        self,
        *,
        cwd: str | None,
        domain: str | None = None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        root = canonical(Path(domain)) if domain else self._source_root(cwd)
        if not _safe_directory(root):
            return []
        expected_cwd = str(Path(cwd).expanduser().resolve()) if cwd else None
        rows: list[TranscriptCandidate] = []
        for path in root.glob("*.jsonl"):
            header = _header(path)
            if header is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            reason = None
            if after is not None and stat.st_ctime < after:
                reason = "created before participant floor"
            elif expected_cwd is not None and _header_cwd(header) != expected_cwd:
                reason = "cwd mismatch"
            rows.append(
                TranscriptCandidate(
                    location=str(canonical(path)),
                    session_id=header["id"],
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    rejection_reason=reason,
                    domain=str(root),
                )
            )
        return sorted(rows, key=lambda row: (row.mtime or 0, row.location), reverse=True)

    def admit_operator_candidate(
        self,
        *,
        cwd: str | None,
        candidate: str,
        domain: str | None = None,
        after: float | None = None,
    ) -> TranscriptCandidate:
        root = canonical(Path(domain)) if domain else self._source_root(cwd)
        path = canonical(Path(candidate))
        if path.is_symlink() or not path.is_relative_to(root):
            raise ValueError("candidate path is outside this harness transcript domain")
        header = _header(path)
        if header is None:
            raise ValueError("harness shape mismatch")
        stat = path.stat()
        if after is not None and stat.st_ctime < after:
            raise ValueError("created before participant floor")
        if cwd is not None and _header_cwd(header) != str(Path(cwd).expanduser().resolve()):
            raise ValueError("cwd mismatch")
        return TranscriptCandidate(
            location=str(path),
            session_id=header["id"],
            mtime=stat.st_mtime,
            size=stat.st_size,
            domain=str(root),
        )


def _safe_directory(path: Path) -> bool:
    try:
        stat = path.lstat()
    except OSError:
        return False
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    return path.is_dir() and not path.is_symlink() and (euid is None or stat.st_uid == euid)


def source_factory(context: ParticipantObservationContext, *, root: Path | None = None) -> Source:
    return PiObserver(root=root).open_source_context(context)


def transcript_candidates(
    context: TranscriptCandidatesContext, *, root: Path | None = None
) -> list[TranscriptCandidate]:
    return PiObserver(root=root).transcript_candidates(
        cwd=context.cwd,
        domain=context.domain,
        after=context.after,
    )


def admit_operator_candidate(
    context: OperatorCandidateContext, *, root: Path | None = None
) -> TranscriptCandidate:
    return PiObserver(root=root).admit_operator_candidate(
        cwd=context.cwd,
        candidate=context.candidate,
        domain=context.domain,
        after=context.after,
    )
