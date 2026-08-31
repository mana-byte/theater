"""Claude transcript discovery and lifecycle receipt validation.

The lossy directory slug is never reconstructed; records provide the trusted cwd.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from glob import escape
from pathlib import Path
from typing import NoReturn

from theater.harness.source import TranscriptCandidate
from theater.harness.transcript.discovery import GlobDiscovery, stat_birthtime
from theater.models import BadRequest
from theater.provenance import TranscriptProvenance

from .constants import (
    _CWD_PROBE_BYTES,
    _CWD_PROBE_RECORDS,
    _LOSS_CANDIDATE_PROBES,
    _RESUME_CWD_SUFFIX_BYTES,
    _RESUME_CWD_SUFFIX_RECORDS,
)
from .launch import _hook_string
from .source import _ClaudeSource, _open_claude_source


def authoritative_resume_cwd(
    *, root: Path, session_id: str | None, known_location: str | None
) -> str:
    """Return the current project cwd recorded by one durable Claude session."""
    if not session_id:
        raise BadRequest("cannot resume Claude session: Theater has no native session id")
    transcript = materialized_resume_transcript(
        root=root,
        session_id=session_id,
        known_location=known_location,
    )
    return _current_transcript_cwd(transcript, session_id)


def materialized_resume_transcript(
    *, root: Path, session_id: str | None, known_location: str | None
) -> Path:
    """Return one exact materialized Claude transcript without reading its contents."""
    if not session_id:
        raise BadRequest("cannot resume Claude session: Theater has no native session id")
    domain = root.expanduser().resolve(strict=False)
    candidates: list[Path] = []
    if known_location is not None:
        candidate = _materialized_candidate(domain, Path(known_location).expanduser(), session_id)
        if candidate is not None:
            candidates.append(candidate)
    if domain.is_dir():
        for path in domain.glob(f"*/{escape(session_id)}.jsonl"):
            candidate = _materialized_candidate(domain, path, session_id)
            if candidate is not None and candidate not in candidates:
                candidates.append(candidate)
    if not candidates:
        return _missing_resume_transcript(session_id, domain)
    if len(candidates) != 1:
        raise BadRequest(
            f"cannot resume Claude session {session_id!r}: multiple native transcripts "
            f"exist under {str(domain)!r}; resolve the duplicate before retrying."
        )
    return candidates[0]


def _materialized_candidate(root: Path, path: Path, session_id: str) -> Path | None:
    if path.is_symlink() or not path.is_file():
        return None
    canonical = path.resolve(strict=False)
    try:
        relative = canonical.relative_to(root)
    except ValueError:
        return None
    if canonical.suffix != ".jsonl" or len(relative.parts) != 2 or canonical.stem != session_id:
        return None
    return canonical


def _missing_resume_transcript(session_id: str, root: Path) -> NoReturn:
    raise BadRequest(
        f"cannot resume Claude session {session_id!r}: its native transcript has not "
        f"materialized under {str(root)!r}. The recorded id may be only a planned "
        "launch id; start a new Claude session instead."
    )


def unique_relocation_candidate(*, root: Path, session_id: str) -> Path | None:
    """The one file this exact session id now lives at, or ``None``.

    A non-throwing wrapper around :func:`materialized_resume_transcript`'s
    validated enumeration: zero matches (not yet materialized, or genuinely
    gone) and more than one match (an unresolved collision) both come back as
    ``None`` rather than raising, because a vanished-pin recovery attempt must
    fail closed onto the ordinary quarantine path, not surface a resume-style
    error.
    """
    try:
        return materialized_resume_transcript(root=root, session_id=session_id, known_location=None)
    except BadRequest:
        return None


def _current_transcript_cwd(path: Path, session_id: str) -> str:
    suffix = _resume_cwd_suffix(path, session_id)
    for raw in reversed(_complete_suffix_lines(suffix)):
        record = _json_record(raw)
        if record is None:
            continue
        _validate_resume_record_session(record, path, session_id)
        if cwd := _resume_record_cwd(record, path, session_id):
            return cwd
    if not suffix:
        raise BadRequest(
            f"cannot resume Claude session {session_id!r}: native transcript {str(path)!r} "
            "has no complete records in its bounded suffix"
        )
    raise BadRequest(
        f"cannot resume Claude session {session_id!r}: native transcript {str(path)!r} "
        "does not record a usable project cwd in its bounded suffix"
    )


def _resume_cwd_suffix(path: Path, session_id: str) -> bytes:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - _RESUME_CWD_SUFFIX_BYTES)
            fh.seek(start)
            suffix = fh.read(_RESUME_CWD_SUFFIX_BYTES)
    except OSError as exc:
        raise BadRequest(
            f"cannot resume Claude session {session_id!r}: native transcript {str(path)!r} "
            f"is not readable: {exc}"
        ) from exc
    if start:
        _partial, separator, suffix = suffix.partition(b"\n")
        if not separator:
            suffix = b""
    return suffix


def _complete_suffix_lines(suffix: bytes) -> list[bytes]:
    lines = suffix.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    return lines[-_RESUME_CWD_SUFFIX_RECORDS:]


def _json_record(raw: bytes) -> dict | None:
    try:
        record = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _validate_resume_record_session(record: dict, path: Path, session_id: str) -> None:
    for key in ("session_id", "sessionId"):
        found_session = record.get(key)
        if isinstance(found_session, str) and found_session and found_session != session_id:
            raise BadRequest(
                f"cannot resume Claude session {session_id!r}: transcript {str(path)!r} "
                "records a different native session id"
            )


def _resume_record_cwd(record: dict, path: Path, session_id: str) -> str | None:
    found_cwd = record.get("cwd")
    if not isinstance(found_cwd, str) or not found_cwd:
        return None
    candidate = Path(found_cwd).expanduser()
    if not candidate.is_absolute():
        raise BadRequest(
            f"cannot resume Claude session {session_id!r}: transcript {str(path)!r} "
            "records a relative cwd"
        )
    current = str(candidate.resolve(strict=False))
    if not Path(current).is_dir():
        raise BadRequest(
            f"cannot resume Claude session {session_id!r}: transcript project cwd "
            f"{current!r} no longer exists"
        )
    return current


class ClaudeIdentity:
    """Transcript layout is ``~/.claude/projects/<slugged-cwd>/<session>.jsonl``."""

    root: Path
    relocate_by_cwd: bool

    @property
    def _discovery(self) -> GlobDiscovery:
        return GlobDiscovery(
            root=self.root,
            glob_pattern="*/*.jsonl",
            session_id_of=self._session_id_of,
            cwd_of=self._transcript_cwd,
            is_shape=self._is_claude_shape,
            birthtime_of=stat_birthtime,
            loss_probes=_LOSS_CANDIDATE_PROBES,
            collision_warning=(
                "claude find_transcript: %d transcripts match cwd %s; "
                "returning the newest — the observer will refuse a collision"
            ),
        )

    @staticmethod
    def _session_id_of(path: Path) -> str | None:
        return path.stem if path.suffix == ".jsonl" and path.stem else None

    @staticmethod
    def _is_claude_shape(path: Path, *, root: Path) -> bool:
        if path.suffix != ".jsonl":
            return False
        return path.parent.parent.resolve() == root.resolve()

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> _ClaudeSource:
        return _open_claude_source(
            root=self.root,
            relocate_by_cwd=self.relocate_by_cwd,
            cwd=cwd,
            session_id=session_id,
            after=after,
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
        return _open_claude_source(
            root=self.root,
            relocate_by_cwd=self.relocate_by_cwd,
            cwd=cwd,
            session_id=session_id,
            after=after,
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
        return self._discovery.find_transcript(cwd=cwd, after=after)

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

    def exact_relocation_candidate(self, *, session_id: str) -> Path | None:
        return unique_relocation_candidate(root=self.root, session_id=session_id)

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
                    if i >= _CWD_PROBE_RECORDS:
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
