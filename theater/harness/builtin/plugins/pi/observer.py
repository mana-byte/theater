"""Pi transcript discovery and source wiring."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from theater.harness.contracts.callbacks import (
    OperatorCandidateContext,
    ScreenContext,
    TranscriptCandidatesContext,
)
from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.source import Source, TranscriptCandidate
from theater.harness.normalization.timing import iso_epoch
from theater.harness.observation import ScreenKind, ScreenReading, TranscriptObserver
from theater.provenance import TranscriptProvenance
from theater.trajectory.capabilities import TrajectoryCapabilities, TrajectoryFeature

from .constants import (
    PI_HEADER_BYTES,
    PI_SESSIONS_DIRNAME,
    PI_SWITCH_MARKER,
    PI_SWITCH_MARKER_BYTES,
    PI_SWITCH_MARKER_VERSION,
    PI_SWITCHES_DIRNAME,
)
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


def _file_started(stat: os.stat_result) -> float | None:
    """Return an immutable creation time when the platform exposes one."""
    started = getattr(stat, "st_birthtime", None)
    return float(started) if isinstance(started, (int, float)) else None


def _session_started(header: dict, stat: os.stat_result) -> float:
    """Use Pi's immutable session timestamp, not ctime which changes on append."""
    started = iso_epoch(header.get("timestamp"))
    if started is not None:
        return started
    # Linux has no creation time in stat.  ctime is deliberately not a
    # fallback: an append mutates it, allowing an old transcript to masquerade
    # as a newly created session.
    return _file_started(stat) or 0.0


def _optional_nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(frozen=True, slots=True)
class PiSwitchBoundary:
    """A Pi-authored session replacement and its pre-existing history boundary."""

    location: Path
    previous_location: Path
    reason: str
    offset: int | None
    records: int | None
    dev: int | None
    ino: int | None


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
        source_checkpoint: str | None = None,
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
            source_checkpoint=source_checkpoint,
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
        source_checkpoint: str | None = None,
    ) -> Source:
        domain = (
            canonical(Path(transcript_domain))
            if transcript_domain
            else participant_root(participant_id)
        )
        marker = validate_domain(domain, participant_id=participant_id)
        # A migrated checkpoint may still reference a predecessor-owned domain.
        if marker is not None or (
            source_checkpoint is not None and validate_domain(domain) is not None
        ):
            return PiObserver(root=domain, isolated=True).open_source(
                cwd=cwd,
                session_id=session_id,
                after=after,
                session_provenance=session_provenance,
                known_location=known_location,
                source_checkpoint=source_checkpoint,
            )
        return self.open_source(
            cwd=cwd,
            session_id=session_id,
            after=after,
            session_provenance=session_provenance,
            known_location=known_location,
            source_checkpoint=source_checkpoint,
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
            source_checkpoint=context.source_checkpoint,
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
            if after is not None and _session_started(header, stat) < after:
                continue
            candidates.append((stat.st_mtime_ns, canonical(path)))
        return max(candidates, default=(0, None))[1]

    def switch_boundary(  # noqa: PLR0912
        self, *, cwd: str, current: Path | None = None, target: Path | None = None
    ) -> PiSwitchBoundary | None:
        """Read the bounded handoff written by Theater's bundled Pi extension."""
        root = self._source_root(cwd)
        if not _safe_directory(root):
            return None
        marker = root / PI_SWITCH_MARKER
        if target is not None:
            digest = hashlib.sha256(str(canonical(target)).encode()).hexdigest()
            archived = root / PI_SWITCHES_DIRNAME / f"{digest}.json"
            if archived.is_file() and not archived.is_symlink():
                marker = archived
        try:
            marker_stat = marker.lstat()
            if (
                marker.is_symlink()
                or not marker.is_file()
                or marker_stat.st_size > PI_SWITCH_MARKER_BYTES
            ):
                return None
            with marker.open("rb") as stream:
                raw = stream.read(PI_SWITCH_MARKER_BYTES + 1)
        except OSError:
            return None
        if len(raw) > PI_SWITCH_MARKER_BYTES:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(value, dict) or value.get("version") != PI_SWITCH_MARKER_VERSION:
            return None
        if value.get("reason") not in {"new", "resume", "fork", "startup-fork"}:
            return None
        location_raw = value.get("location")
        previous_raw = value.get("previous_location")
        if not isinstance(location_raw, str) or not isinstance(previous_raw, str):
            return None
        location = canonical(Path(location_raw))
        previous = canonical(Path(previous_raw))
        if not location.is_relative_to(root):
            return None
        if value["reason"] != "startup-fork" and not previous.is_relative_to(root):
            return None
        if current is not None and previous != canonical(current):
            return None
        if location == previous or location.is_symlink() or not location.is_file():
            return None
        header = _header(location)
        if header is None or _header_cwd(header) != str(Path(cwd).expanduser().resolve()):
            return None
        if value["reason"] == "startup-fork":
            parent = header.get("parentSession")
            if not isinstance(parent, str) or canonical(Path(parent)) != previous:
                return None
        offset = _optional_nonnegative_int(value.get("offset"))
        records = _optional_nonnegative_int(value.get("records"))
        dev = _optional_nonnegative_int(value.get("dev"))
        ino = _optional_nonnegative_int(value.get("ino"))
        if value["reason"] == "new":
            offset = records = 0
            dev = ino = None
        elif (offset is None and records is None) or (dev is None) != (ino is None):
            return None
        return PiSwitchBoundary(
            location=location,
            previous_location=previous,
            reason=value["reason"],
            offset=offset,
            records=records,
            dev=dev,
            ino=ino,
        )

    def is_fork_transcript(self, transcript: Path) -> bool:
        header = _header(transcript)
        return header is not None and isinstance(header.get("parentSession"), str)

    def find_switch_transcript(self, *, cwd: str, current: Path) -> Path | None:
        boundary = self.switch_boundary(cwd=cwd, current=current)
        return None if boundary is None else boundary.location

    def session_id(self, transcript: Path) -> str | None:
        header = _header(transcript)
        return header["id"] if header is not None else None

    def session_started(self, transcript: Path) -> float:
        header = _header(transcript)
        if header is None:
            return 0.0
        try:
            stat = transcript.stat()
        except OSError:
            return 0.0
        return _session_started(header, stat)

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
            if after is not None and _session_started(header, stat) < after:
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
        if after is not None and _session_started(header, stat) < after:
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
