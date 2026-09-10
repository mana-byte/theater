"""Encode and decode the persisted resume floor.

A resume floor is a structured JSON fact recording the stream position of a
trusted dead predecessor's transcript at the last safe pre-launch moment.
The spawner captures it before the successor launches and persists it on the
successor's participant row; the observer reads it at watcher start and uses
it to suppress stale pre-floor records.

Encoding is structured JSON with validation, not a bare string. There are two
shapes, and they never mix:

* **File floor** — the legacy four fields (``records``, ``size``, ``dev``,
  ``ino``), all optional. This is what an append-only transcript produces.
  A floor with missing facts is present-but-unknown.
* **Logical floor** — a *versioned* shape carrying ``stream_id`` and
  ``position`` for a source backed by a mutable store that has no stable
  file identity (no ``dev``/``ino``). The version tag (``"v"``) lets a future
  reader distinguish the shapes and refuse an unknown one fail-closed.

The string ``UNKNOWN_FLOOR`` distinguishes "the spawner tried but could not
capture facts" (suppress completion) from a ``None`` floor (cold spawn, no
suppression).

Mutable logical sources
-----------------------
A mutable store cannot offer inode continuity: rotating the store or
rewriting a row moves the watermark with no file-identity change to prove the
successor is reading the *same* stream the predecessor left. The logical
floor substitutes a stable opaque ``stream_id`` plus a monotone
``position`` watermark for that proof. Because a single point must not carry
both file and logical identity, the comparison fails closed on any mix — a
point that tries to be both is treated as malformed rather than guessed at.

The comparison logic lives here rather than in the observer so the policy
is testable without constructing a full observer: given a floor and an
attachment's :class:`~theater.harness.source.StreamPoint`, the floor
authorises completion only when every guard passes.
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

    ``None`` means the source could not produce facts. The spawner still
    persists a floor (``UNKNOWN_FLOOR``) so the reducer knows to suppress
    rather than treat this as a cold spawn.

    File points keep the legacy four-field shape exactly. A logical point
    (one carrying ``stream_id`` and ``position``) encodes a *versioned*
    shape containing only those two fields — file and logical identity are
    never written into the same record. A point that carries both regimes
    is malformed and is persisted as ``UNKNOWN_FLOOR`` so the reducer
    suppresses completion rather than guessing which identity to honour.
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
    """Whether *value* is a real int (not bool) and non-negative.

    ``bool`` is a subclass of ``int`` in Python, so ``isinstance(True, int)``
    is ``True``. A JSON ``true`` decoded as a Python ``bool`` is not a valid
    record count or byte offset, and accepting it would let a corrupt floor
    authorise completion on a non-numeric fact.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_position(value: object) -> bool:
    """Whether *value* is a real non-negative int usable as a logical watermark.

    Same numeric rules as :func:`_valid_int`: ``bool`` is not a valid
    position, and a negative position can never prove the stream moved
    strictly beyond the floor.
    """
    return _valid_int(value)


def _valid_stream_id(value: object) -> bool:
    """Whether *value* is a non-empty bounded string usable as a stream identity.

    A logical floor believes a stream is the same one the predecessor left
    only by matching this opaque id, so it must be a real string, non-empty,
    and within Theater's identifier byte bound. Anything else (a number, a
    bool, an empty string, an over-long blob) is treated as missing so the
    reducer suppresses rather than authorising on a corrupt identity.
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
    """Decode a persisted floor string back into a StreamPoint.

    Returns ``None`` when the raw value is ``None`` (cold spawn, no floor) or
    ``UNKNOWN_FLOOR`` (present-but-unknown — the spawner tried but could not
    capture facts). The caller distinguishes the two by checking ``raw``
    directly: ``None`` means cold spawn, ``UNKNOWN_FLOOR`` means present but
    unknown.

    A corrupt or malformed JSON string is treated as unknown rather than
    raising: the floor was persisted, and the worst outcome of a parse error
    is over-suppression, which is strictly safer than under-suppression.

    A logical floor (the versioned shape) is decoded only when its version
    is recognised, its ``stream_id`` and ``position`` are valid, and it
    carries no file identity — any deviation (an unknown version, a missing
    or malformed field, or a hybrid record mixing logical and file keys) is
    present-but-unknown and fails closed.
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
    """Whether a persisted floor value means 'suppress completion'.

    ``None`` is a cold spawn — no floor, no suppression.
    ``UNKNOWN_FLOOR`` is present-but-unknown — suppress.
    Any other string is a structured floor — suppress unless authorised.
    """
    return raw is not None


def floor_is_unknown(raw: str | None) -> bool:
    """Whether a persisted floor is present but has no usable facts."""
    return raw == UNKNOWN_FLOOR


def _point_regime(point: StreamPoint) -> str:
    """Classify a point as ``"logical"``, ``"file"``, or ``"mixed"``.

    ``"logical"`` carries logical identity (``stream_id`` or ``position``)
    and no file fields; ``"file"`` carries only file fields (the legacy
    shape, possibly partial); ``"mixed"`` carries both and is never
    authorised.
    """
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
    """Whether an attachment's stream point proves it is past the floor.

    The floor was captured from a dead predecessor's transcript at the last
    safe pre-launch moment. The successor's first attachment must prove it
    is the same stream and has moved strictly beyond the floor.

    Two regimes are supported and never mixed:

    * **File floor** — the legacy proof. Same opaque identity (device and
      inode match), non-shrunk size (``point.size >= floor.size``), and
      strictly more records (``point.records > floor.records``). All four
      facts must be present on both sides; any missing fact refuses.

    * **Logical floor** — for a mutable store with no file identity. Both
      sides must carry *complete* logical identity (a valid non-empty
      ``stream_id`` and a non-negative ``position``), the stream ids must
      be identical, and ``point.position`` must be strictly greater than
      ``floor.position``.

    Fail-closed rules:

    * A point that carries both logical and file identity is *mixed* and
      never authorises — the reducer refuses to guess which identity to
      believe.
    * One side logical and the other file is a cross-regime mismatch and
      never authorises.
    * A logical point missing either field, or with an invalid stream id
      or position, never authorises.
    * A present-but-unknown floor (``floor_raw == UNKNOWN_FLOOR`` or any
      required field missing) never authorises.

    Returns ``False`` when any guard fails or any fact is missing. Returns
    ``True`` only when every guard passes — or when ``floor_raw is None``
    (cold spawn, no floor to prove past).
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
