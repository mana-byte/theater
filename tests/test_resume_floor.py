"""Tests for the resume floor encode/decode and authorisation logic."""

from __future__ import annotations

import json

from theater.harness.source import StreamPoint
from theater.resume_floor import (
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
