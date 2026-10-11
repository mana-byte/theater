"""A job's structured result, falling back to its raw text when it is not strict JSON."""

from __future__ import annotations

from theater.json_values import decode_finite_json
from theater.models import Job


def structured_job_result(job: Job) -> object:
    """The parsed structured result, or the raw result for anything non-finite or invalid."""
    if job.structured_status != "parsed" or job.structured_result is None:
        return job.result
    ok, value = decode_finite_json(job.structured_result)
    return value if ok else job.result


__all__ = ["structured_job_result"]
