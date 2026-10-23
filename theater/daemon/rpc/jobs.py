"""Private RPC adapters and compatibility names for job operations."""

from __future__ import annotations

import time

from theater.daemon import job_operations
from theater.daemon.rpc.router import method

AWAIT_ANNOUNCE_AFTER = job_operations.AWAIT_ANNOUNCE_AFTER
DEFAULT_MAX_WAIT = job_operations.DEFAULT_MAX_WAIT
MAX_AWAIT = job_operations.MAX_AWAIT
_JOB_ERROR_MESSAGES = job_operations._JOB_ERROR_MESSAGES
_job_to_dict = job_operations._job_to_dict
_await_announced = job_operations._await_announced
_open_await = job_operations._open_await
_close_await = job_operations._close_await


@method("jobs.await")
async def _jobs_await(daemon, params: dict) -> list[dict]:
    return await job_operations.await_jobs(daemon, params, clock=time)


@method("jobs.status")
async def _jobs_status(daemon, params: dict) -> dict:
    return await job_operations.job_status(daemon, params)
