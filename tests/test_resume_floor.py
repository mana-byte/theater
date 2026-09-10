"""Tests for the resume floor encode/decode and authorisation logic."""

from __future__ import annotations

import json

from theater.harness.source import StreamPoint
from theater.resume_floor import (
    LOGICAL_FLOOR_VERSION,
    UNKNOWN_FLOOR,
    decode_floor,
    encode_floor,
    floor_authorises_completion,
    floor_is_present,
    floor_is_unknown,
)


def test_encode_none_is_unknown():
    """A None StreamPoint encodes as the unknown sentinel, not null."""
    assert encode_floor(None) == UNKNOWN_FLOOR


def test_encode_point_is_json():
    """A StreamPoint encodes as structured JSON with all four fields."""
    point = StreamPoint(records=5, size=100, dev=10, ino=20)
    encoded = encode_floor(point)
    data = json.loads(encoded)
    assert data["records"] == 5
    assert data["size"] == 100
    assert data["dev"] == 10
    assert data["ino"] == 20


def test_decode_none_is_none():
    """A None raw value decodes to None (cold spawn)."""
    assert decode_floor(None) is None


def test_decode_unknown_is_none():
    """The unknown sentinel decodes to None (present but no facts)."""
    assert decode_floor(UNKNOWN_FLOOR) is None


def test_decode_structured_returns_point():
    """A structured JSON floor decodes to a StreamPoint."""
    point = StreamPoint(records=5, size=100, dev=10, ino=20)
    encoded = encode_floor(point)
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.records == 5
    assert decoded.size == 100
    assert decoded.dev == 10
    assert decoded.ino == 20


def test_decode_corrupt_json_is_none():
    """A corrupt JSON string is treated as unknown, not an error."""
    assert decode_floor("not json") is None


def test_decode_partial_point():
    """A floor with only some fields decodes, with missing fields as None."""
    encoded = json.dumps({"records": 5})
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.records == 5
    assert decoded.size is None
    assert decoded.dev is None
    assert decoded.ino is None


def test_floor_is_present_none_is_false():
    """None means no floor (cold spawn)."""
    assert not floor_is_present(None)


def test_floor_is_present_unknown_is_true():
    """The unknown sentinel is present."""
    assert floor_is_present(UNKNOWN_FLOOR)


def test_floor_is_present_structured_is_true():
    """A structured floor is present."""
    assert floor_is_present(encode_floor(StreamPoint(records=1)))


def test_floor_is_unknown_none_is_false():
    """None is not 'unknown' — it is cold spawn."""
    assert not floor_is_unknown(None)


def test_floor_is_unknown_sentinel_is_true():
    assert floor_is_unknown(UNKNOWN_FLOOR)


def test_floor_is_unknown_structured_is_false():
    assert not floor_is_unknown(encode_floor(StreamPoint(records=1)))


# ---- authorisation: basic guards ------------------------------------------


def test_authorise_null_floor_allows():
    """A NULL floor (cold spawn) always authorises."""
    assert floor_authorises_completion(None, floor_raw=None, point=None) is True


def test_authorise_unknown_floor_refuses():
    """A present-but-unknown floor never authorises."""
    point = StreamPoint(records=10, size=200, dev=1, ino=2)
    assert floor_authorises_completion(None, floor_raw=UNKNOWN_FLOOR, point=point) is False


def test_authorise_same_stream_beyond_floor():
    """Same dev/ino, larger size, more records -> authorised."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    point = StreamPoint(records=10, size=200, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is True


def test_authorise_different_dev_refuses():
    """Different device -> not the same stream."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    point = StreamPoint(records=10, size=200, dev=99, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_different_ino_refuses():
    """Different inode -> not the same stream."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    point = StreamPoint(records=10, size=200, dev=10, ino=99)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_shrunk_size_refuses():
    """Size smaller than floor -> truncation."""
    floor = StreamPoint(records=5, size=200, dev=10, ino=20)
    point = StreamPoint(records=10, size=100, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_equal_records_refuses():
    """Records not strictly greater -> not beyond the floor."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    point = StreamPoint(records=5, size=100, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_fewer_records_refuses():
    """Records fewer than floor -> not beyond."""
    floor = StreamPoint(records=10, size=100, dev=10, ino=20)
    point = StreamPoint(records=5, size=100, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_none_point_refuses():
    """No point on the attachment -> no proof -> refuse."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=None) is False


# ---- B1: fail-closed on missing facts -------------------------------------


def test_authorise_missing_dev_on_floor_refuses():
    """Floor without dev -> cannot prove identity -> refuse."""
    floor = StreamPoint(records=5, size=100, dev=None, ino=20)
    point = StreamPoint(records=10, size=200, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_missing_ino_on_floor_refuses():
    """Floor without ino -> cannot prove identity -> refuse."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=None)
    point = StreamPoint(records=10, size=200, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_missing_records_on_floor_refuses():
    """Floor without records -> cannot prove beyond -> refuse."""
    floor = StreamPoint(records=None, size=100, dev=10, ino=20)
    point = StreamPoint(records=10, size=200, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_missing_size_on_floor_refuses():
    """Floor without size -> cannot prove non-shrunk -> refuse."""
    floor = StreamPoint(records=5, size=None, dev=10, ino=20)
    point = StreamPoint(records=10, size=200, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_missing_dev_on_point_refuses():
    """Point without dev -> cannot prove identity -> refuse."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    point = StreamPoint(records=10, size=200, dev=None, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_missing_ino_on_point_refuses():
    """Point without ino -> cannot prove identity -> refuse."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    point = StreamPoint(records=10, size=200, dev=10, ino=None)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_missing_records_on_point_refuses():
    """Point without records -> cannot prove beyond -> refuse."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    point = StreamPoint(records=None, size=200, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_missing_size_on_point_refuses():
    """Point without size -> cannot prove non-shrunk -> refuse."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    point = StreamPoint(records=10, size=None, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_missing_size_on_both_refuses():
    """Missing size on both floor and point: fail-closed, not authorised."""
    floor = StreamPoint(records=5, size=None, dev=10, ino=20)
    point = StreamPoint(records=10, size=None, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


# ---- B1: corrupt numeric values -------------------------------------------


def test_decode_rejects_bool_records():
    """A JSON true in records is treated as missing, not 1."""
    encoded = json.dumps({"records": True, "size": 100, "dev": 10, "ino": 20})
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.records is None


def test_decode_rejects_bool_size():
    """A JSON true in size is treated as missing."""
    encoded = json.dumps({"records": 5, "size": True, "dev": 10, "ino": 20})
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.size is None


def test_decode_rejects_negative_records():
    """A negative record count is treated as missing."""
    encoded = json.dumps({"records": -1, "size": 100, "dev": 10, "ino": 20})
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.records is None


def test_decode_rejects_negative_size():
    """A negative size is treated as missing."""
    encoded = json.dumps({"records": 5, "size": -100, "dev": 10, "ino": 20})
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.size is None


def test_decode_rejects_negative_dev():
    """A negative dev is treated as missing."""
    encoded = json.dumps({"records": 5, "size": 100, "dev": -10, "ino": 20})
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.dev is None


def test_decode_rejects_string_records():
    """A string in a numeric field is treated as missing."""
    encoded = json.dumps({"records": "five", "size": 100, "dev": 10, "ino": 20})
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.records is None
    assert decoded.size == 100


def test_decode_rejects_float_records():
    """A float in records is treated as missing — only int is valid."""
    encoded = json.dumps({"records": 5.0, "size": 100, "dev": 10, "ino": 20})
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.records is None


def test_decode_zero_is_valid():
    """Zero is a valid non-negative int."""
    encoded = json.dumps({"records": 0, "size": 0, "dev": 0, "ino": 0})
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.records == 0
    assert decoded.size == 0
    assert decoded.dev == 0
    assert decoded.ino == 0


# ---- logical (mutable-store) floors -------------------------------------


def _logical_point(stream_id: str = "sess-1", position: int = 5) -> StreamPoint:
    return StreamPoint(stream_id=stream_id, position=position)


def test_encode_logical_point_is_versioned_json():
    """A logical point encodes a versioned shape with only stream_id and position."""
    point = _logical_point("sess-1", 5)
    encoded = encode_floor(point)
    data = json.loads(encoded)
    assert data["v"] == LOGICAL_FLOOR_VERSION
    assert data["stream_id"] == "sess-1"
    assert data["position"] == 5
    # No file identity fields leak into a logical floor.
    assert "records" not in data and "size" not in data
    assert "dev" not in data and "ino" not in data


def test_encode_logical_point_omits_file_fields():
    """A logical floor never carries file identity fields."""
    encoded = encode_floor(_logical_point("s", 1))
    assert "dev" not in encoded and "ino" not in encoded
    assert "records" not in encoded and "size" not in encoded


def test_encode_mixed_point_is_unknown():
    """A point carrying both logical and file identity fails closed at encode."""
    mixed = StreamPoint(stream_id="s", position=1, dev=10, ino=20)
    assert encode_floor(mixed) == UNKNOWN_FLOOR
    mixed2 = StreamPoint(stream_id="s", position=1, records=5)
    assert encode_floor(mixed2) == UNKNOWN_FLOOR


def test_encode_invalid_logical_is_unknown():
    """A logical point with an empty stream id or bad position fails closed."""
    assert encode_floor(StreamPoint(stream_id="", position=5)) == UNKNOWN_FLOOR
    assert encode_floor(StreamPoint(stream_id="s", position=-1)) == UNKNOWN_FLOOR
    assert encode_floor(StreamPoint(stream_id="s", position=True)) == UNKNOWN_FLOOR
    assert encode_floor(StreamPoint(stream_id=123, position=5)) == UNKNOWN_FLOOR  # type: ignore[arg-type]


def test_decode_logical_floor_roundtrips():
    """A versioned logical floor decodes back to a logical StreamPoint."""
    point = _logical_point("sess-1", 5)
    decoded = decode_floor(encode_floor(point))
    assert decoded is not None
    assert decoded.stream_id == "sess-1"
    assert decoded.position == 5
    # File fields stay None on a decoded logical floor.
    assert decoded.records is None and decoded.size is None
    assert decoded.dev is None and decoded.ino is None


def test_decode_logical_floor_is_present_not_unknown():
    raw = encode_floor(_logical_point("s", 1))
    assert floor_is_present(raw)
    assert not floor_is_unknown(raw)


def test_decode_unknown_logical_version_is_none():
    """An unrecognised version tag is present-but-unknown (fail closed)."""
    raw = json.dumps({"v": 99, "stream_id": "s", "position": 5})
    assert decode_floor(raw) is None


def test_decode_unversioned_logical_keys_is_none():
    """Logical keys without a version tag are malformed and fail closed."""
    raw = json.dumps({"stream_id": "s", "position": 5})
    assert decode_floor(raw) is None


def test_decode_logical_missing_fields_is_none():
    raw = json.dumps({"v": LOGICAL_FLOOR_VERSION, "stream_id": "s"})
    assert decode_floor(raw) is None
    raw = json.dumps({"v": LOGICAL_FLOOR_VERSION, "position": 5})
    assert decode_floor(raw) is None


def test_decode_logical_bad_stream_id_is_none():
    raw = json.dumps({"v": LOGICAL_FLOOR_VERSION, "stream_id": "", "position": 5})
    assert decode_floor(raw) is None
    raw = json.dumps({"v": LOGICAL_FLOOR_VERSION, "stream_id": 5, "position": 5})
    assert decode_floor(raw) is None


def test_decode_logical_bad_position_is_none():
    raw = json.dumps({"v": LOGICAL_FLOOR_VERSION, "stream_id": "s", "position": -1})
    assert decode_floor(raw) is None
    raw = json.dumps({"v": LOGICAL_FLOOR_VERSION, "stream_id": "s", "position": True})
    assert decode_floor(raw) is None
    raw = json.dumps({"v": LOGICAL_FLOOR_VERSION, "stream_id": "s", "position": 5.0})
    assert decode_floor(raw) is None


def test_decode_logical_floor_zero_position_is_valid():
    """Zero is a valid non-negative position."""
    raw = json.dumps({"v": LOGICAL_FLOOR_VERSION, "stream_id": "s", "position": 0})
    decoded = decode_floor(raw)
    assert decoded is not None
    assert decoded.position == 0
    assert decoded.stream_id == "s"


def test_decode_mixed_keys_is_none():
    """A persisted record mixing logical and file keys fails closed."""
    raw = json.dumps({"v": LOGICAL_FLOOR_VERSION, "stream_id": "s", "position": 1, "records": 5})
    assert decode_floor(raw) is None
    raw = json.dumps({"stream_id": "s", "position": 1, "dev": 10, "ino": 20})
    assert decode_floor(raw) is None


def test_decode_legacy_floor_still_roundtrips():
    """Legacy file floors decode unchanged alongside the new logical shape."""
    point = StreamPoint(records=5, size=100, dev=10, ino=20)
    decoded = decode_floor(encode_floor(point))
    assert decoded is not None
    assert decoded.records == 5 and decoded.size == 100
    assert decoded.dev == 10 and decoded.ino == 20
    assert decoded.stream_id is None and decoded.position is None


# ---- authorisation: logical regime ---------------------------------------


def test_authorise_logical_advance():
    """Same stream id, strictly greater position -> authorised."""
    floor = _logical_point("sess-1", 5)
    point = _logical_point("sess-1", 10)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is True


def test_authorise_logical_equal_position_refuses():
    """Position not strictly greater -> not beyond the floor."""
    floor = _logical_point("sess-1", 5)
    point = _logical_point("sess-1", 5)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_logical_backward_position_refuses():
    """Position behind the floor -> not beyond."""
    floor = _logical_point("sess-1", 10)
    point = _logical_point("sess-1", 5)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_logical_wrong_stream_refuses():
    """Different stream id -> not the same stream."""
    floor = _logical_point("sess-1", 5)
    point = _logical_point("sess-2", 10)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_logical_empty_stream_refuses():
    """An empty stream id on the point cannot prove identity."""
    floor = _logical_point("sess-1", 5)
    point = StreamPoint(stream_id="", position=10)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_logical_missing_position_refuses():
    """A logical point without a position is incomplete and fails closed."""
    floor = _logical_point("sess-1", 5)
    point = StreamPoint(stream_id="sess-1", position=None)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_logical_none_point_refuses():
    floor = _logical_point("sess-1", 5)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=None) is False


def test_authorise_logical_unknown_floor_refuses():
    point = _logical_point("sess-1", 10)
    assert floor_authorises_completion(None, floor_raw=UNKNOWN_FLOOR, point=point) is False


def test_authorise_logical_null_floor_allows():
    """A cold spawn (None floor) always authorises, even for a logical point."""
    assert floor_authorises_completion(None, floor_raw=None, point=_logical_point("s", 1)) is True


# ---- authorisation: mixed regimes fail closed -----------------------------


def test_authorise_mixed_point_refuses():
    """A point carrying both logical and file identity never authorises."""
    floor = _logical_point("sess-1", 5)
    mixed = StreamPoint(stream_id="sess-1", position=10, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=mixed) is False


def test_authorise_mixed_floor_refuses():
    """A floor carrying both regimes never authorises (it encodes as unknown)."""
    mixed = StreamPoint(stream_id="sess-1", position=5, dev=10, ino=20)
    point = StreamPoint(dev=10, ino=20, records=10, size=200)
    # encode_floor persists a mixed floor as UNKNOWN_FLOOR.
    assert floor_authorises_completion(mixed, floor_raw=encode_floor(mixed), point=point) is False


def test_authorise_logical_floor_file_point_refuses():
    """A logical floor against a file point is a cross-regime mismatch."""
    floor = _logical_point("sess-1", 5)
    point = StreamPoint(records=10, size=200, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_file_floor_logical_point_refuses():
    """A file floor against a logical point is a cross-regime mismatch."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    point = _logical_point("sess-1", 10)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


def test_authorise_file_regime_unchanged():
    """Sanity: a pure file floor/point pair still uses the legacy comparison."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    point = StreamPoint(records=10, size=200, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is True
    bad = StreamPoint(records=10, size=200, dev=99, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=bad) is False
