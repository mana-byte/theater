"""Public transcript, recall, and trajectory reads over daemon-owned services."""

from __future__ import annotations

from types import MappingProxyType

from theater.constants.daemon import (
    RECALL_READ_RESPONSE_MAX_BYTES,
    TRANSCRIPT_READ_RESPONSE_MAX_BYTES,
)
from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.daemon import workers
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.recall_chunks import recall_chunk as _recall_chunk
from theater.daemon.frontend.transcript_projection import public_transcript_event
from theater.daemon.frontend.validation import PublicRequestError
from theater.daemon.operations import OperationService
from theater.daemon.recall import recall_query as _recall_query
from theater.daemon.rpc.transcripts import (
    complete_transcript_bind_result,
    persist_transcript_bind,
    prepare_transcript_bind,
    read_transcript_page,
    transcript_candidates,
)
from theater.daemon.trajectory.responses import resync_delta
from theater.frontend.capabilities import METHOD_CATALOG, PUBLIC_LIMITS
from theater.frontend.schemas import validator_for
from theater.models import NotFound

_PAGE_MAX = 500
_RECALL_CURSOR_PREFIX = "recall1:"


def _validated(method: str, result: dict[str, object]) -> dict[str, object]:
    validator_for(METHOD_CATALOG[method].result_schema_id).validate(result)
    return result


def _operations(daemon) -> OperationService:
    service = getattr(daemon, "operation_service", None)
    if not isinstance(service, OperationService):
        raise TypeError("daemon operation service is not composed")
    return service


async def _complete_public_transcript_bind(daemon, value: object) -> dict[str, object]:
    """Repair live observer state from the durable idempotency result."""
    if not isinstance(value, dict):
        raise TypeError("stored frontend.transcripts.bind result is invalid")
    await complete_transcript_bind_result(daemon, value)
    return value


async def transcripts_read(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    return await read_transcript_page(
        daemon,
        participant_id=params["participant_id"],
        cursor=params.get("cursor"),
        max_bytes=params.get("max_bytes", TRANSCRIPT_READ_RESPONSE_MAX_BYTES),
        event_projection=public_transcript_event,
    )


async def transcripts_candidates(
    daemon, _context: ConnectionContext, params: dict
) -> dict[str, object]:
    participant_id = params["participant_id"]
    try:
        participant = daemon.registry.get(participant_id)
    except NotFound as exc:
        raise PublicRequestError(
            "not_found", f"no participant {participant_id!r}", {"participant_id": participant_id}
        ) from exc
    items = await transcript_candidates(daemon, participant)
    if len(items) > _PAGE_MAX:
        # This endpoint predates cursor input, so rejecting an oversized archive
        # makes every candidate unusable.  Keep the useful rows first and bound
        # the response instead.  Python's stable sort preserves the observer's
        # newest-first ordering inside each group.
        items.sort(key=lambda item: item.get("rejection_reason") is not None)
        del items[_PAGE_MAX:]
    return _validated("frontend.transcripts.candidates", {"items": items, "next_cursor": None})


async def transcripts_bind(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> dict[str, object]:
    operations = _operations(daemon)
    replay = operations.replay_idempotent_write(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        method="frontend.transcripts.bind",
        params=params,
    )
    if replay is not None:
        replay_value = await _complete_public_transcript_bind(daemon, replay.value)
        return _validated("frontend.transcripts.bind", replay_value)
    prepared = await prepare_transcript_bind(
        daemon,
        participant_id=params["participant_id"],
        location=params["location"],
        prior_owner_id=params.get("prior_owner_id"),
        actor_surface="frontend",
        actor_client_id=context.client_id,
    )
    value: dict[str, object] = {
        "participant_id": prepared.target.id,
        "location": prepared.location,
        "session_id": prepared.session_id,
        "prior_owner_id": None if prepared.prior_owner is None else prepared.prior_owner.id,
    }

    def action(unit) -> dict[str, object]:
        persist_transcript_bind(
            daemon,
            prepared,
            unit=unit,
        )
        return value

    outcome = operations.execute_idempotent(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        method="frontend.transcripts.bind",
        params=params,
        action=action,
    )
    value = await _complete_public_transcript_bind(daemon, outcome.value)
    return _validated("frontend.transcripts.bind", value)


def _recall_offset(cursor: object) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.startswith(_RECALL_CURSOR_PREFIX):
        raise PublicRequestError("bad_request", "recall cursor is malformed")
    value = cursor.removeprefix(_RECALL_CURSOR_PREFIX)
    if not value.isdecimal():
        raise PublicRequestError("bad_request", "recall cursor is malformed")
    return int(value)


def _attach_parent_names(daemon, result: dict[str, dict]) -> None:
    """Add live aliases without changing durable recall ownership facts."""
    for timeline in result.values():
        points = timeline.get("timeline", [])
        if not isinstance(points, list):
            continue
        for point in points:
            if not isinstance(point, dict):
                continue
            parent_id = point.get("parent_id")
            if parent_id is None:
                continue
            try:
                point["parent_name"] = daemon.registry.get(parent_id).name
            except Exception:
                point["parent_name"] = None


async def recall_query(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    limit = params.get("limit", 200)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _PAGE_MAX:
        raise PublicRequestError("bad_request", "recall page limit must be between 1 and 500")
    result = await _recall_query(daemon.store, paths=[params["path"]], depth=_PAGE_MAX)
    _attach_parent_names(daemon, result)
    if len(result) != 1:
        raise PublicRequestError("internal", "recall did not return exactly one requested path")
    path, timeline = next(iter(result.items()))
    points = timeline.get("timeline")
    if not isinstance(points, list):
        raise PublicRequestError("internal", "recall result has no timeline")
    offset = _recall_offset(params.get("cursor"))
    if offset >= len(points) and params.get("cursor") is not None:
        raise PublicRequestError("bad_request", "recall cursor is outside the current timeline")
    page = points[offset : offset + limit]
    items = [{"path": path, **point} for point in page]
    next_cursor = (
        f"{_RECALL_CURSOR_PREFIX}{offset + len(page)}" if offset + len(page) < len(points) else None
    )
    metadata = {key: value for key, value in timeline.items() if key != "timeline"}
    return _validated(
        "frontend.recall.query",
        {
            "items": items,
            "next_cursor": next_cursor,
            "path": path,
            "metadata": metadata,
            "source_limited": len(points) == _PAGE_MAX,
        },
    )


async def recall_read(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    from theater.daemon.recall_read import read_segment

    result = await read_segment(
        params["segment_id"],
        store=daemon.store,
        registry=daemon.registry,
        cwd=".",
        observer=daemon.observer,
    )
    if "offset" not in params and "max_bytes" not in params:
        return result
    offset = params.get("offset", 0)
    max_bytes = params.get("max_bytes", RECALL_READ_RESPONSE_MAX_BYTES)
    if type(offset) is not int or type(max_bytes) is not int or max_bytes <= 0:
        raise PublicRequestError("bad_request", "recall read offset and max_bytes must be integers")
    return await workers.to_thread(
        _recall_chunk,
        params["segment_id"],
        result,
        offset=offset,
        max_bytes=max_bytes,
        label="recall_read.chunk",
    )


def _trajectory_stream(daemon, stream_id: str):
    for stream in daemon.trajectory.streams.values():
        if stream.cache.stream_id == stream_id:
            return stream
    return None


async def trajectory_snapshot(
    daemon, _context: ConnectionContext, params: dict
) -> dict[str, object]:
    page = await daemon.trajectory.snapshot(
        params["participant_id"],
        before=params.get("before"),
        limit=params.get("limit", TRAJECTORY_PAGE_RECORD_LIMIT),
    )
    # The router validates the complete response once at the public boundary.
    return page.to_wire()


async def trajectory_follow(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    stream_id = params["stream_id"]
    stream = _trajectory_stream(daemon, stream_id)
    if stream is None:
        return resync_delta(
            stream_id,
            "the trajectory stream is not warm; request a fresh snapshot",
        ).to_wire()
    delta = await daemon.trajectory.follow(
        stream.participant.id,
        stream_id=stream_id,
        after=params["cursor"],
        wait=params.get("wait_seconds", float(PUBLIC_LIMITS["follow_wait_seconds"])),
        limit=TRAJECTORY_PAGE_RECORD_LIMIT,
    )
    return delta.to_wire()


async def trajectory_close(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    stream_id = params["stream_id"]
    stream = _trajectory_stream(daemon, stream_id)
    released = (
        False
        if stream is None
        else daemon.trajectory.close_viewer(stream.participant.id, stream_id)
    )
    return {"stream_id": stream_id, "released": released}


async def trajectory_locate(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    result = daemon.trajectory.locate(params["participant_id"], params["record_id"])
    return result.to_wire()


async def trajectory_search(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    result = await daemon.trajectory.search(
        params["participant_id"],
        query=params["query"],
        limit=params.get("limit", TRAJECTORY_PAGE_RECORD_LIMIT),
    )
    wire = result.to_wire()
    records = wire.pop("records")
    return {"items": records, "next_cursor": None, **wire}


OBSERVATION_HANDLERS = MappingProxyType(
    {
        "frontend.transcripts.read": transcripts_read,
        "frontend.transcripts.candidates": transcripts_candidates,
        "frontend.transcripts.bind": transcripts_bind,
        "frontend.recall.query": recall_query,
        "frontend.recall.read": recall_read,
        "frontend.trajectory.snapshot": trajectory_snapshot,
        "frontend.trajectory.follow": trajectory_follow,
        "frontend.trajectory.close": trajectory_close,
        "frontend.trajectory.locate": trajectory_locate,
        "frontend.trajectory.search": trajectory_search,
    }
)

__all__ = ["OBSERVATION_HANDLERS"]
