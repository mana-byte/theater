"""Encode and decode the persisted resume floor.

A resume floor is a structured JSON fact recording the stream position of a
trusted dead predecessor's transcript at the last safe pre-launch moment.
The spawner captures it before the successor launches and persists it on the
successor's participant row; the observer reads it at watcher start and uses
it to suppress stale pre-floor records.

Encoding is structured JSON with validation, not a bare string: the four
fields (records, size, dev, ino) are all optional, and a floor with missing
facts is present-but-unknown. The string ``UNKNOWN_FLOOR`` distinguishes "the
spawner tried but could not capture facts" (suppress completion) from a
``None`` floor (cold spawn, no suppression).

The comparison logic lives here rather than in the observer so the policy
is testable without constructing a full observer: given a floor and an
attachment's :class:`~theater.harness.source.StreamPoint`, the floor
authorises completion only when every guard passes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from theater.harness.source import StreamPoint

#: The string stored when the spawner captured a floor but could not produce
#: file facts (non-file source, unreadable file, etc.). Distinct from ``None``
#: (no floor at all — cold spawn) so the reducer suppresses completion rather
#: than treating the successor as a fresh start.
UNKNOWN_FLOOR = "unknown"


def encode_floor(point: StreamPoint | None) -> str:
    """Encode a StreamPoint as a JSON string for persistence.

    ``None`` means the source could not produce facts. The spawner still
    persists a floor (``UNKNOWN_FLOOR``) so the reducer knows to suppress
    rather than treat this as a cold spawn.
    """
    if point is None:
        return UNKNOWN_FLOOR
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


def floor_authorises_completion(
    floor: StreamPoint | None,
    *,
    floor_raw: str | None,
    point: StreamPoint | None,
) -> bool:
    """Whether an attachment's stream point proves it is past the floor.

    The floor was captured from a dead predecessor's transcript at the last
    safe pre-launch moment. The successor's first attachment must prove it is
    the same stream and has moved strictly beyond the floor:

    * Same opaque identity (device and inode match). Different location,
      device, or inode means truncation, rewrite, or a different file.
    * Non-shrunk size: the byte offset must be >= the floor's size.
    * Strictly beyond the saved record count: ``point.records > floor.records``.

    Fail-closed: **all four facts** (dev, ino, records, size) must be present
    on **both** the floor and the point. A present-but-unknown floor
    (``floor_raw == UNKNOWN_FLOOR`` or any field missing) never authorises.
    Missing facts on either side refuse — the reducer suppresses rather than
    guessing.

    Returns ``False`` when any guard fails or any fact is missing. Returns
    ``True`` only when every guard passes.
    """
    if floor_raw is None:
        return True
    if floor_raw == UNKNOWN_FLOOR or floor is None:
        return False
    if point is None:
        return False
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
