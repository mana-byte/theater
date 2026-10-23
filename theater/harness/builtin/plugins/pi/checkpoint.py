"""Pi's persisted byte cursor and legacy resume-floor inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from theater.harness.contracts.source import StreamPoint
from theater.resume_floor import decode_floor


@dataclass(frozen=True, slots=True)
class ExactCheckpoint:
    location: str
    point: StreamPoint
    raw: str


@dataclass(frozen=True, slots=True)
class LegacyFloor:
    point: StreamPoint


@dataclass(frozen=True, slots=True)
class ColdResume:
    pass


@dataclass(frozen=True, slots=True)
class UnknownResume:
    pass


ResumeInput = ExactCheckpoint | LegacyFloor | ColdResume | UnknownResume


def _checkpoint_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def make_checkpoint(
    path: Path, offset: int, records: int, dev: int | None, ino: int | None
) -> ExactCheckpoint | None:
    if dev is None or ino is None:
        return None
    raw = json.dumps(
        {
            "version": 1,
            "location": str(path),
            "offset": offset,
            "records": records,
            "dev": dev,
            "ino": ino,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return ExactCheckpoint(
        str(path), StreamPoint(size=offset, records=records, dev=dev, ino=ino), raw
    )


def decode_checkpoint(raw: str | None) -> ExactCheckpoint | None:
    if raw is None or len(raw) > 1024:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    location = value.get("location")
    if not isinstance(location, str) or not location:
        return None
    offset = _checkpoint_int(value.get("offset"))
    records = _checkpoint_int(value.get("records"))
    dev = _checkpoint_int(value.get("dev"))
    ino = _checkpoint_int(value.get("ino"))
    if offset is None or records is None or dev is None or ino is None:
        return None
    return ExactCheckpoint(
        location, StreamPoint(size=offset, records=records, dev=dev, ino=ino), raw
    )


def decode_resume(raw: str | None) -> ResumeInput:
    if raw is None:
        return ColdResume()
    if checkpoint := decode_checkpoint(raw):
        return checkpoint
    floor = decode_floor(raw)
    if floor is not None and _complete_file_point(floor, require_records=True):
        return LegacyFloor(floor)
    return UnknownResume()


def _complete_file_point(point: StreamPoint, *, require_records: bool) -> bool:
    return (
        point.dev is not None
        and point.ino is not None
        and point.size is not None
        and (not require_records or point.records is not None)
    )


def file_point_covers(
    current: StreamPoint, required: StreamPoint, *, require_records: bool = True
) -> bool:
    """Compare file identity and byte floor; rollback deliberately ignores records."""
    if not _complete_file_point(
        current, require_records=require_records
    ) or not _complete_file_point(required, require_records=require_records):
        return False
    assert current.dev is not None and current.ino is not None and current.size is not None
    assert required.dev is not None and required.ino is not None and required.size is not None
    if current.dev != required.dev or current.ino != required.ino or current.size < required.size:
        return False
    if not require_records:
        return True
    assert current.records is not None and required.records is not None
    return current.records >= required.records
