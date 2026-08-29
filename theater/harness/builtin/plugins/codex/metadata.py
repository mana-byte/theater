"""Bounded classification of Codex rollout metadata."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .constants import _CWD_PROBE_BYTES, CODEX_SESSION_META_RECORD_TYPE

SUBAGENT_REJECTION = "codex native subagent rollout"
UNKNOWN_AUTOMATIC_REJECTION = "unrecognized codex rollout metadata; explicit binding required"


class RolloutKind(Enum):
    PRIMARY = "primary"
    LEGACY = "legacy"
    SUBAGENT = "subagent"
    UNKNOWN = "unknown"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class RolloutMetadata:
    cwd: str | None
    kind: RolloutKind

    @property
    def automatic_rejection(self) -> str | None:
        if self.kind is RolloutKind.SUBAGENT:
            return SUBAGENT_REJECTION
        if self.kind is RolloutKind.UNKNOWN:
            return UNKNOWN_AUTOMATIC_REJECTION
        return None

    @property
    def binding_rejection(self) -> str | None:
        return SUBAGENT_REJECTION if self.kind is RolloutKind.SUBAGENT else None


def read_rollout_metadata(path: Path) -> RolloutMetadata:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            line = fh.readline(_CWD_PROBE_BYTES)
    except OSError:
        return RolloutMetadata(None, RolloutKind.MALFORMED)
    try:
        record = json.loads(line)
    except ValueError:
        return RolloutMetadata(None, RolloutKind.MALFORMED)
    if not isinstance(record, dict) or record.get("type") != CODEX_SESSION_META_RECORD_TYPE:
        return RolloutMetadata(None, RolloutKind.MALFORMED)
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return RolloutMetadata(None, RolloutKind.MALFORMED)

    found = payload.get("cwd")
    try:
        cwd = str(Path(found).resolve()) if isinstance(found, str) and found else None
    except (OSError, ValueError):
        cwd = None

    source = payload.get("source")
    if payload.get("thread_source") == "subagent" or (
        isinstance(source, Mapping) and "subagent" in source
    ):
        return RolloutMetadata(cwd, RolloutKind.SUBAGENT)
    if cwd is None:
        return RolloutMetadata(None, RolloutKind.MALFORMED)
    if "thread_source" not in payload and "source" not in payload:
        return RolloutMetadata(cwd, RolloutKind.LEGACY)
    if payload.get("thread_source") == "user" and isinstance(source, str):
        return RolloutMetadata(cwd, RolloutKind.PRIMARY)
    return RolloutMetadata(cwd, RolloutKind.UNKNOWN)
