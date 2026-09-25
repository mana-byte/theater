"""Encode, decode, and compare a trusted dead predecessor's persisted resume floor.
Two never-mixed shapes: file (records/size/dev/ino) and versioned logical (stream_id/position).
``UNKNOWN_FLOOR`` suppresses, ``None`` is a cold spawn; any mix or malformation fails closed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES

if TYPE_CHECKING:
    from theater.harness.source import StreamPoint

#: Spawner captured a floor but could not produce file facts. Distinct from None (cold spawn).
UNKNOWN_FLOOR = "unknown"

#: Version tag written on logical (mutable-store) floors so a reader can
#: distinguish them from the legacy file shape and refuse an unknown version
#: fail-closed instead of guessing.
LOGICAL_FLOOR_VERSION = 2


def encode_floor(point: StreamPoint | None) -> str:
    """Encode a StreamPoint as a JSON string for persistence.
    ``None`` and hybrid file+logical points persist as ``UNKNOWN_FLOOR`` so completion stays
    suppressed.
    """
    if point is None:
        return UNKNOWN_FLOOR
    has_logical = point.stream_id is not None or point.position is not None
    has_file = (
        point.records is not None
        or point.size is not None
        or point.dev is not None
        or point.ino is not None
    )
    if has_logical and has_file:
        # Mixed point — refuse to persist a hybrid identity; fail closed.
        return UNKNOWN_FLOOR
    if has_logical:
        if not _valid_stream_id(point.stream_id) or not _valid_position(point.position):
            return UNKNOWN_FLOOR
        return json.dumps(
            {
                "v": LOGICAL_FLOOR_VERSION,
                "stream_id": point.stream_id,
                "position": point.position,
            },
            sort_keys=True,
        )
    return json.dumps(
        {
            "records": point.records,
            "size": point.size,
            "dev": point.dev,
            "ino": point.ino,
        },
        sort_keys=True,
    )


def _valid_int(value: object) -> bool:
    """Whether *value* is a non-negative int and not a bool (JSON ``true`` is no count)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_position(value: object) -> bool:
    """Whether *value* is a non-negative, non-bool int usable as a logical watermark."""
    return _valid_int(value)


def _valid_stream_id(value: object) -> bool:
    """Whether *value* is a non-empty, bounded string usable as a stream identity.

    Anything else counts as missing so a corrupt identity suppresses rather than authorises.
    """
    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= TRAJECTORY_IDENTIFIER_MAX_BYTES


def _has_logical_keys(data: dict[str, object]) -> bool:
    return "stream_id" in data or "position" in data or "v" in data


def _has_file_keys(data: dict[str, object]) -> bool:
    return any(key in data for key in ("records", "size", "dev", "ino"))


def decode_floor(raw: str | None) -> StreamPoint | None:
    """Decode a persisted floor string back into a StreamPoint, or None.
    ``None``/``UNKNOWN_FLOOR`` return None (check ``raw``); corrupt, unknown-version, or hybrid
    floors are unknown, since over-suppression is strictly safer.
    """
    if raw is None or raw == UNKNOWN_FLOOR:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    from theater.harness.source import StreamPoint

    has_logical_keys = _has_logical_keys(data)
    has_file_keys = _has_file_keys(data)
    if has_logical_keys and has_file_keys:
        # A persisted record mixing regimes is malformed — refuse it.
        return None
    if has_logical_keys:
        if data.get("v") != LOGICAL_FLOOR_VERSION:
            # Unknown or unversioned logical shape — fail closed.
            return None
        stream_id = data.get("stream_id")
        position = data.get("position")
        if not _valid_stream_id(stream_id) or not _valid_position(position):
            return None
        return StreamPoint(stream_id=stream_id, position=position)

    return StreamPoint(
        records=data.get("records") if _valid_int(data.get("records")) else None,
        size=data.get("size") if _valid_int(data.get("size")) else None,
        dev=data.get("dev") if _valid_int(data.get("dev")) else None,
        ino=data.get("ino") if _valid_int(data.get("ino")) else None,
    )


def floor_is_present(raw: str | None) -> bool:
    """Whether a persisted floor means 'suppress completion': anything but ``None`` (cold spawn)."""
    return raw is not None


def floor_is_unknown(raw: str | None) -> bool:
    """Whether a persisted floor is present but has no usable facts."""
    return raw == UNKNOWN_FLOOR


def _point_regime(point: StreamPoint) -> str:
    """Classify a point as ``"logical"``, ``"file"``, or ``"mixed"`` (never authorised)."""
    has_logical = point.stream_id is not None or point.position is not None
    has_file = (
        point.records is not None
        or point.size is not None
        or point.dev is not None
        or point.ino is not None
    )
    if has_logical and has_file:
        return "mixed"
    if has_logical:
        return "logical"
    return "file"


def _logical_complete(point: StreamPoint) -> bool:
    """Whether a logical point carries a valid stream id and position."""
    return _valid_stream_id(point.stream_id) and _valid_position(point.position)


def floor_authorises_completion(
    floor: StreamPoint | None,
    *,
    floor_raw: str | None,
    point: StreamPoint | None,
) -> bool:
    """Whether an attachment's stream point proves the same stream, strictly past the floor.
    File: dev/ino match, size not shrunk, more records. Logical: same stream_id, greater position.
    Mixed, cross-regime, incomplete, or unknown floors never authorise; ``floor_raw is None`` does.
    """
    if floor_raw is None:
        return True
    if floor_raw == UNKNOWN_FLOOR or floor is None:
        return False
    if point is None:
        return False

    floor_regime = _point_regime(floor)
    point_regime = _point_regime(point)
    if floor_regime == "mixed" or point_regime == "mixed":
        return False
    if floor_regime == "logical" and point_regime == "logical":
        if not _logical_complete(floor) or not _logical_complete(point):
            return False
        floor_stream_id = floor.stream_id
        point_stream_id = point.stream_id
        floor_position = floor.position
        point_position = point.position
        assert floor_stream_id is not None and point_stream_id is not None
        assert floor_position is not None and point_position is not None
        if floor_stream_id != point_stream_id:
            return False
        return point_position > floor_position
    if floor_regime == "file" and point_regime == "file":
        # All four facts required on both sides — fail-closed on any missing.
        if floor.dev is None or floor.ino is None or floor.records is None or floor.size is None:
            return False
        if point.dev is None or point.ino is None or point.records is None or point.size is None:
            return False
        if point.dev != floor.dev or point.ino != floor.ino:
            return False
        if point.size < floor.size:
            return False
        return point.records > floor.records
    # One side logical, the other file — a cross-regime mismatch, fail closed.
    return False
