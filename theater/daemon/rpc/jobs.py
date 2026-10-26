"""Private RPC adapters and compatibility names for job operations."""

from __future__ import annotations

import time

from theater.daemon import job_operations
from theater.daemon.rpc.router import method

AWAIT_ANNOUNCE_AFTER = job_operations.AWAIT_ANNOUNCE_AFTER
MAX_AWAIT = job_operations.MAX_AWAIT
_JOB_ERROR_MESSAGES = job_operations._JOB_ERROR_MESSAGES
_job_to_dict = job_operations._job_to_dict


@method("jobs.await")
async def _jobs_await(daemon, params: dict) -> list[dict]:
    from theater.daemon import methods as _facade

    return await job_operations.await_jobs(
        daemon,
        params,
        clock=time,
        max_await=lambda: _facade.MAX_AWAIT,
        announce_after=lambda: _facade.AWAIT_ANNOUNCE_AFTER,
    )


@method("jobs.status")
async def _jobs_status(daemon, params: dict) -> dict:
    return await job_operations.job_status(daemon, params)
