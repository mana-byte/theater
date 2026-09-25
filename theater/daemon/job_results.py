"""A job's structured result, falling back to its raw text when it is not strict JSON."""

from __future__ import annotations

import json
import math

from theater.models import Job


def structured_job_result(job: Job) -> object:
    """The parsed structured result, or the raw result for anything non-finite or invalid."""
    if job.structured_status != "parsed" or job.structured_result is None:
        return job.result
    try:
        value = json.loads(job.structured_result, parse_constant=_reject_nonfinite)
    except (TypeError, ValueError, RecursionError):
        return job.result
    return value if _finite(value) else job.result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _finite(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True


__all__ = ["structured_job_result"]
