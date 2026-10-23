"""Characterization for the shared DTO scalar readers and precise string typing."""

from __future__ import annotations

import math

import pytest

from theater.frontend.dto._wire import optional_number, string_value
from theater.frontend.dto.jobs import Job
from theater.frontend.dto.workspaces import WorkspaceUsage


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (0, 0.0),
        (7, 7.0),
        (2.5, 2.5),
        (math.inf, math.inf),
        (-math.inf, -math.inf),
    ],
)
def test_optional_number_reads_null_numbers_and_nonfinite_floats(
    value: object, expected: float | None
) -> None:
    result = optional_number(value, "job.created_at")
    assert result == expected
    if type(value) is int:
        assert isinstance(result, float)


def test_optional_number_accepts_nan() -> None:
    result = optional_number(math.nan, "job.created_at")
    assert result is not None
    assert math.isnan(result)


@pytest.mark.parametrize("value", [True, False, "7", [7], object()])
def test_optional_number_rejects_booleans_and_non_numbers(value: object) -> None:
    with pytest.raises(TypeError, match=r"job\.created_at must be a number or null"):
        optional_number(value, "job.created_at")


def test_string_value_requires_a_non_empty_string() -> None:
    assert string_value("x", "field") == "x"
    for value in (None, "", 5):
        with pytest.raises(TypeError, match="field must be a non-empty string"):
            string_value(value, "field")


def test_string_value_optional_reads_null_but_still_rejects_empty() -> None:
    assert string_value(None, "field", optional=True) is None
    assert string_value("x", "field", optional=True) == "x"
    with pytest.raises(TypeError, match="field must be a non-empty string"):
        string_value("", "field", optional=True)


def _job_wire(**overrides: object) -> dict[str, object]:
    wire: dict[str, object] = {"handle": "job-a", "state": "running", "kind": "send"}
    wire.update(overrides)
    return wire


def test_job_reads_shared_optional_timestamps() -> None:
    job = Job.from_wire(_job_wire(created_at=5, finished_at=None))
    assert job.created_at == 5.0
    assert isinstance(job.created_at, float)
    assert job.finished_at is None


def test_job_accepts_nonfinite_timestamps() -> None:
    job = Job.from_wire(_job_wire(created_at=math.inf))
    assert job.created_at == math.inf


@pytest.mark.parametrize("value", [True, "now"])
def test_job_rejects_non_numeric_timestamps(value: object) -> None:
    with pytest.raises(TypeError, match=r"job\.created_at must be a number or null"):
        Job.from_wire(_job_wire(created_at=value))


def test_job_required_strings_raise_instead_of_falling_back_to_empty() -> None:
    with pytest.raises(TypeError, match=r"job\.handle must be a non-empty string"):
        Job.from_wire(_job_wire(handle=""))
    with pytest.raises(TypeError, match=r"job\.handle must be a non-empty string"):
        Job.from_wire({"state": "running", "kind": "send"})


def _usage_wire(**overrides: object) -> dict[str, object]:
    wire: dict[str, object] = {
        "workspace_id": "ws-a",
        "holder_kind": "participant",
        "holder_id": "participant-a",
        "acquired_at": 1.0,
    }
    wire.update(overrides)
    return wire


def test_workspace_numbers_keep_their_own_error_wording() -> None:
    with pytest.raises(TypeError, match=r"usage\.acquired_at must be a number$"):
        WorkspaceUsage.from_wire(_usage_wire(acquired_at="soon"))
    with pytest.raises(TypeError, match=r"usage\.released_at must be a number$"):
        WorkspaceUsage.from_wire(_usage_wire(released_at="soon"))
    with pytest.raises(TypeError, match=r"usage\.acquired_at must be a number$"):
        WorkspaceUsage.from_wire(_usage_wire(acquired_at=True))


def test_workspace_optional_release_time_reads_null_and_converts() -> None:
    assert WorkspaceUsage.from_wire(_usage_wire(released_at=None)).released_at is None
    released = WorkspaceUsage.from_wire(_usage_wire(released_at=3)).released_at
    assert released == 3.0
    assert isinstance(released, float)
