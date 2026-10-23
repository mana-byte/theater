"""Named trajectory requests shared by private RPC and authenticated plugins."""

from __future__ import annotations

from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_FOLLOW_TIMEOUT_SECONDS,
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_PAGE_RECORD_LIMIT,
    TRAJECTORY_SEARCH_QUERY_MAX_BYTES,
    TRAJECTORY_SEARCH_RESULT_LIMIT,
)
from theater.daemon.trajectory.params import (
    rpc_optional_cursor,
    rpc_optional_stream_id,
    rpc_required_bounded_string,
    rpc_required_identifier,
    rpc_required_string,
    validate_limit,
    validate_rpc_wait,
)


async def snapshot(daemon, params: dict):
    method_name = "trajectory.snapshot"
    participant_id = rpc_required_string(params, "id", method_name)
    before = rpc_optional_cursor(params, "before", method_name)
    limit = validate_limit(params.get("limit", TRAJECTORY_PAGE_RECORD_LIMIT), method_name)
    return await daemon.trajectory.snapshot(participant_id, before=before, limit=limit)


async def follow(daemon, params: dict):
    method_name = "trajectory.follow"
    participant_id = rpc_required_string(params, "id", method_name)
    stream_id = rpc_required_bounded_string(
        params, "stream_id", method_name, TRAJECTORY_IDENTIFIER_MAX_BYTES
    )
    after = rpc_required_bounded_string(params, "after", method_name, TRAJECTORY_CURSOR_MAX_BYTES)
    wait = validate_rpc_wait(params.get("wait", TRAJECTORY_FOLLOW_TIMEOUT_SECONDS), method_name)
    limit = validate_limit(params.get("limit", TRAJECTORY_PAGE_RECORD_LIMIT), method_name)
    return await daemon.trajectory.follow(
        participant_id, stream_id=stream_id, after=after, wait=wait, limit=limit
    )


def close(daemon, params: dict) -> bool:
    method_name = "trajectory.close"
    participant_id = rpc_required_string(params, "id", method_name)
    stream_id = rpc_optional_stream_id(params, method_name)
    return daemon.trajectory.close_viewer(participant_id, stream_id)


def locate(daemon, params: dict):
    method_name = "trajectory.locate"
    participant_id = rpc_required_identifier(params, "id", method_name)
    record_id = rpc_required_identifier(params, "record_id", method_name)
    return daemon.trajectory.locate(participant_id, record_id)


async def search(daemon, params: dict):
    method_name = "trajectory.search"
    participant_id = rpc_required_identifier(params, "id", method_name)
    query = rpc_required_bounded_string(
        params, "query", method_name, TRAJECTORY_SEARCH_QUERY_MAX_BYTES
    )
    limit = validate_limit(params.get("limit", TRAJECTORY_SEARCH_RESULT_LIMIT), method_name)
    return await daemon.trajectory.search(participant_id, query=query, limit=limit)
