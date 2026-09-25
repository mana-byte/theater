"""Public job projections and waits over the existing job coordinator."""

from __future__ import annotations

from types import MappingProxyType

from sqlalchemy import and_, or_, select

from theater.daemon.awaiting import REASON_TIMEOUT, coordinate_await, parse_targets, snapshot_for
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.validation import PublicRequestError
from theater.daemon.job_results import structured_job_result
from theater.daemon.rpc.jobs import _JOB_ERROR_MESSAGES
from theater.daemon.schema import jobs
from theater.frontend.capabilities import METHOD_CATALOG, PUBLIC_LIMITS
from theater.frontend.schemas import validator_for
from theater.models import Job

_PAGE_DEFAULT = 200
_PAGE_MAX = 500


def _validated(method: str, result: dict[str, object]) -> dict[str, object]:
    validator_for(METHOD_CATALOG[method].result_schema_id).validate(result)
    return result


def job_to_wire(job: Job) -> dict[str, object]:
    """Keep parsed structured results while retaining raw legacy evidence additively."""
    actor: dict[str, object] | None = None
    legacy_caller_id: str | None = None
    if job.actor_client_id:
        actor = {"client_id": job.actor_client_id, "participant_id": job.actor_participant_id}
    elif job.caller_id:
        legacy_caller_id = job.caller_id
    else:
        raise PublicRequestError(
            "internal",
            f"stored job {job.handle!r} has no actor identity",
            {"job_handle": job.handle},
        )
    error = None
    if job.error_code:
        error = {
            "code": job.error_code,
            "message": _JOB_ERROR_MESSAGES.get(
                job.error_code, f"job finished with error code {job.error_code!r}"
            ),
        }
    return {
        "handle": job.handle,
        "state": str(job.state),
        "kind": str(job.kind),
        "actor": actor,
        "legacy_caller_id": legacy_caller_id,
        "target_id": job.target_id,
        "result": structured_job_result(job),
        "error": error,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "structured_status": job.structured_status,
        "raw_result": job.result,
        "error_code": job.error_code,
        "response_format": job.response_format,
    }


def _list_jobs(daemon, params: dict) -> tuple[list[Job], str | None]:
    limit = params.get("limit", _PAGE_DEFAULT)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _PAGE_MAX:
        raise PublicRequestError("bad_request", "job page limit must be between 1 and 500")
    statement = select(jobs)
    state = params.get("state")
    if state is not None:
        statement = statement.where(jobs.c.state == state)
    participant_id = params.get("participant_id")
    if participant_id is not None:
        statement = statement.where(jobs.c.target_id == participant_id)
    cursor = params.get("cursor")
    if cursor is not None:
        marker = daemon.store.get_job(cursor)
        if marker is None:
            raise PublicRequestError("bad_request", f"unknown job cursor {cursor!r}")
        statement = statement.where(
            or_(
                jobs.c.created_at > marker.created_at,
                and_(jobs.c.created_at == marker.created_at, jobs.c.handle > marker.handle),
            )
        )
    rows = daemon.store.conn.execute(
        statement.order_by(jobs.c.created_at.asc(), jobs.c.handle.asc()).limit(limit + 1)
    ).fetchall()
    items = [Job.from_row(row._mapping) for row in rows]
    has_more = len(items) > limit
    page = items[:limit]
    return page, page[-1].handle if has_more and page else None


async def jobs_list(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    page, next_cursor = _list_jobs(daemon, params)
    return _validated(
        "frontend.jobs.list",
        {"items": [job_to_wire(job) for job in page], "next_cursor": next_cursor},
    )


async def jobs_get(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    handle = params["job_handle"]
    job = daemon.jobs.get(handle)
    if job is None:
        raise PublicRequestError("not_found", f"no job {handle!r}", {"job_handle": handle})
    return _validated("frontend.jobs.get", job_to_wire(job))


async def jobs_await(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    handles = list(params["job_handles"])
    targets = parse_targets(daemon, handles)
    targets_by_handle = {target.handle: target for target in targets}
    missing = [handle for handle in handles if handle not in targets_by_handle]
    non_jobs = [
        handle
        for handle in handles
        if (target := targets_by_handle.get(handle)) is not None and target.job is None
    ]
    if missing or non_jobs:
        unknown = sorted({*missing, *non_jobs})
        raise PublicRequestError(
            "not_found",
            f"no job(s): {', '.join(unknown)}",
            {"job_handles": unknown},
        )
    wait_seconds = params.get("wait_seconds", float(PUBLIC_LIMITS["follow_wait_seconds"]))
    reasons = await coordinate_await(daemon, targets, max_wait=float(wait_seconds))
    items = []
    for target in targets:
        assert target.job is not None
        value = job_to_wire(target.job)
        if target.target_id is not None:
            presence = target.presence or snapshot_for(
                getattr(daemon, "presence", None), target.target_id
            )
            value["human_presence"] = presence.to_dict()
            participant = daemon.store.get_participant(target.target_id)
            value["participant_status"] = None if participant is None else str(participant.status)
        else:
            value["participant_status"] = None
        value["await_reason"] = reasons.get(target.handle, REASON_TIMEOUT)
        items.append(value)
    timed_out = all(reasons.get(target.handle) == REASON_TIMEOUT for target in targets)
    return _validated("frontend.jobs.await", {"jobs": items, "timed_out": timed_out})


JOB_HANDLERS = MappingProxyType(
    {
        "frontend.jobs.list": jobs_list,
        "frontend.jobs.get": jobs_get,
        "frontend.jobs.await": jobs_await,
    }
)

__all__ = ["JOB_HANDLERS", "job_to_wire"]
