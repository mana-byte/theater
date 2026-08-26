"""Focused tests for trajectory boundary parameter validation."""

from __future__ import annotations

import math
import re

import pytest

from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_PAGE_RECORD_LIMIT,
)
from theater.daemon.trajectory.params import (
    rpc_optional_cursor,
    rpc_optional_stream_id,
    rpc_required_bounded_string,
    rpc_required_identifier,
    validate_bounded_string,
    validate_identifier,
    validate_limit,
    validate_optional_cursor,
    validate_participant_token,
    validate_rpc_wait,
    validate_wait,
)
from theater.models import BadRequest


def _message(error: str):
    return pytest.raises(BadRequest, match=f"^{re.escape(error)}$")


@pytest.mark.parametrize(
    ("params", "expected", "error"),
    [
        ({}, None, "trajectory.follow requires non-empty string parameter 'after'"),
        ({"after": "\ud800"}, None, "trajectory.follow parameter 'after' must contain valid UTF-8"),
        (
            {"after": "x" * (TRAJECTORY_CURSOR_MAX_BYTES + 1)},
            None,
            f"trajectory.follow parameter 'after' exceeds "
            f"{TRAJECTORY_CURSOR_MAX_BYTES} encoded bytes",
        ),
        ({"after": "cursor"}, "cursor", None),
    ],
)
def test_rpc_required_bounded_string_boundary_cases(params, expected, error) -> None:
    if error is not None:
        with _message(error):
            rpc_required_bounded_string(
                params, "after", "trajectory.follow", TRAJECTORY_CURSOR_MAX_BYTES
            )
    else:
        assert (
            rpc_required_bounded_string(
                params, "after", "trajectory.follow", TRAJECTORY_CURSOR_MAX_BYTES
            )
            == expected
        )


@pytest.mark.parametrize(
    ("value", "expected", "rpc_error", "service_error"),
    [
        (None, None, None, None),
        (
            "",
            None,
            "trajectory.snapshot parameter 'before' must be a non-empty string or null",
            "trajectory.snapshot parameter 'before' must be a non-empty string or null",
        ),
        (
            "x" * (TRAJECTORY_CURSOR_MAX_BYTES + 1),
            None,
            "trajectory.snapshot parameter 'before' exceeds the cursor limit",
            f"trajectory.snapshot parameter 'before' exceeds "
            f"{TRAJECTORY_CURSOR_MAX_BYTES} encoded bytes",
        ),
        ("cursor", "cursor", None, None),
    ],
)
def test_optional_cursor_boundary_cases(value, expected, rpc_error, service_error) -> None:
    params = {"before": value}
    if rpc_error is None:
        assert rpc_optional_cursor(params, "before", "trajectory.snapshot") == expected
    else:
        with _message(rpc_error):
            rpc_optional_cursor(params, "before", "trajectory.snapshot")
    if service_error is not None:
        with _message(service_error):
            validate_optional_cursor(value, "trajectory.snapshot")
    else:
        validate_optional_cursor(value, "trajectory.snapshot")


def test_stream_and_identifier_validation_preserve_boundary_messages() -> None:
    with _message("trajectory.close parameter 'stream_id' exceeds the identifier limit"):
        rpc_optional_stream_id(
            {"stream_id": "x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1)}, "trajectory.close"
        )
    with _message("trajectory.locate parameter 'record_id' must not contain control characters"):
        rpc_required_identifier({"record_id": "bus:\x00"}, "record_id", "trajectory.locate")
    with _message("trajectory.locate parameter 'record_id' must not contain control characters"):
        validate_identifier("bus:\x00", "trajectory.locate", "record_id")
    with _message("trajectory parameter 'stream_id' exceeds 512 encoded bytes"):
        validate_bounded_string(
            "x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1),
            "stream_id",
            TRAJECTORY_IDENTIFIER_MAX_BYTES,
        )
    with _message("trajectory.snapshot parameter 'id' must contain valid UTF-8"):
        validate_participant_token("\ud800", "trajectory.snapshot")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1),
        (TRAJECTORY_PAGE_RECORD_LIMIT + 1, TRAJECTORY_PAGE_RECORD_LIMIT),
    ],
)
def test_validate_limit_clamps_positive_integers(value, expected) -> None:
    assert validate_limit(value, "trajectory.follow") == expected


@pytest.mark.parametrize("value", [True, 0, -1, 1.0])
def test_validate_limit_rejects_non_positive_integers(value) -> None:
    with _message("trajectory.follow parameter 'limit' must be a positive integer"):
        validate_limit(value, "trajectory.follow")


class _IntegerSubclass(int):
    pass


@pytest.mark.parametrize("value", [True, math.nan, math.inf, -1, "1", _IntegerSubclass(1)])
def test_validate_rpc_wait_rejects_invalid_values(value) -> None:
    with _message("trajectory.follow parameter 'wait' must be a non-negative finite number"):
        validate_rpc_wait(value, "trajectory.follow")


def test_validate_wait_accepts_numeric_subclasses_and_clamps() -> None:
    assert validate_wait(_IntegerSubclass(1), "trajectory.follow") == 1.0
    assert validate_wait(100, "trajectory.follow") == 20.0
