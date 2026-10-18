"""Resume floor encoding, decoding and completion authorisation."""

from __future__ import annotations

import json

import pytest

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


@pytest.mark.parametrize(
    ("raw", "present", "unknown"),
    [
        pytest.param(None, False, False, id="none_is_a_cold_spawn"),
        pytest.param(UNKNOWN_FLOOR, True, True, id="unknown_sentinel"),
        pytest.param(encode_floor(StreamPoint(records=1)), True, False, id="structured"),
    ],
)
def test_floor_presence_and_unknownness(raw, present, unknown):
    assert bool(floor_is_present(raw)) is present
    assert bool(floor_is_unknown(raw)) is unknown


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


def test_authorise_none_point_refuses():
    """No point on the attachment -> no proof -> refuse."""
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=None) is False


_FLOOR = {"records": 5, "size": 100, "dev": 10, "ino": 20}
_POINT = {"records": 10, "size": 200, "dev": 10, "ino": 20}


@pytest.mark.parametrize(
    ("floor_changes", "point_changes"),
    [
        pytest.param({}, {"dev": 99}, id="different_dev"),
        pytest.param({}, {"ino": 99}, id="different_ino"),
        pytest.param({"size": 200}, {"size": 100}, id="shrunk_size"),
        pytest.param({}, {"records": 5, "size": 100}, id="equal_records"),
        pytest.param({"records": 10}, {"records": 5, "size": 100}, id="fewer_records"),
        pytest.param({"dev": None}, {}, id="missing_dev_on_floor"),
        pytest.param({"ino": None}, {}, id="missing_ino_on_floor"),
        pytest.param({"records": None}, {}, id="missing_records_on_floor"),
        pytest.param({"size": None}, {}, id="missing_size_on_floor"),
        pytest.param({}, {"dev": None}, id="missing_dev_on_point"),
        pytest.param({}, {"ino": None}, id="missing_ino_on_point"),
        pytest.param({}, {"records": None}, id="missing_records_on_point"),
        pytest.param({}, {"size": None}, id="missing_size_on_point"),
        pytest.param({"size": None}, {"size": None}, id="missing_size_on_both"),
    ],
)
def test_authorise_refuses_what_cannot_be_proven_beyond_the_floor(floor_changes, point_changes):
    """Different stream, truncation, no advance, or a missing fact: fail closed."""
    floor = StreamPoint(**{**_FLOOR, **floor_changes})
    point = StreamPoint(**{**_POINT, **point_changes})
    assert floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is False


@pytest.mark.parametrize(
    ("changes", "field", "kept"),
    [
        pytest.param({"records": True}, "records", {}, id="bool_records"),
        pytest.param({"size": True}, "size", {}, id="bool_size"),
        pytest.param({"records": -1}, "records", {}, id="negative_records"),
        pytest.param({"size": -100}, "size", {}, id="negative_size"),
        pytest.param({"dev": -10}, "dev", {}, id="negative_dev"),
        pytest.param({"records": "five"}, "records", {"size": 100}, id="string_records"),
        pytest.param({"records": 5.0}, "records", {}, id="float_records"),
    ],
)
def test_decode_rejects_corrupt_numeric_values(changes, field, kept):
    """Bools, negatives, strings and floats are treated as missing, not coerced."""
    decoded = decode_floor(json.dumps({**_FLOOR, **changes}))
    assert decoded is not None
    assert getattr(decoded, field) is None
    for name, value in kept.items():
        assert getattr(decoded, name) == value


def test_decode_zero_is_valid():
    """Zero is a valid non-negative int."""
    encoded = json.dumps({"records": 0, "size": 0, "dev": 0, "ino": 0})
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.records == 0
    assert decoded.size == 0
    assert decoded.dev == 0
    assert decoded.ino == 0


def _logical(stream_id: str = "sess-1", position: int = 5) -> StreamPoint:
    return StreamPoint(stream_id=stream_id, position=position)


def test_logical_floor_round_trips_with_a_distinct_shape():
    encoded = encode_floor(_logical("session", 0))
    data = json.loads(encoded)
    assert data == {"v": LOGICAL_FLOOR_VERSION, "stream_id": "session", "position": 0}
    decoded = decode_floor(encoded)
    assert decoded is not None
    assert decoded.stream_id == "session" and decoded.position == 0
    assert decoded.records is None and decoded.size is None
    assert decoded.dev is None and decoded.ino is None
    assert floor_is_present(encoded) and not floor_is_unknown(encoded)


def test_malformed_or_mixed_logical_floors_fail_closed():
    malformed = [
        json.dumps({"v": 99, "stream_id": "s", "position": 5}),
        json.dumps({"v": LOGICAL_FLOOR_VERSION, "stream_id": "s"}),
        json.dumps({"v": LOGICAL_FLOOR_VERSION, "stream_id": "s", "position": True}),
        json.dumps({"v": LOGICAL_FLOOR_VERSION, "stream_id": "s", "position": 1, "records": 5}),
    ]
    assert all(decode_floor(raw) is None for raw in malformed)
    mixed = StreamPoint(stream_id="sess-1", position=5, dev=10, ino=20)
    assert encode_floor(mixed) == UNKNOWN_FLOOR


def test_logical_completion_requires_same_stream_and_strict_advance():
    floor = _logical("sess-1", 5)
    cases = [
        (_logical("sess-1", 6), True),
        (_logical("sess-1", 5), False),
        (_logical("sess-2", 6), False),
        (StreamPoint(records=6, size=6, dev=1, ino=1), False),
    ]
    assert all(
        floor_authorises_completion(floor, floor_raw=encode_floor(floor), point=point) is expected
        for point, expected in cases
    )
