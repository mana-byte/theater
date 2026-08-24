"""Trajectory snapshot, follow, and best-effort viewer-release RPCs."""

from __future__ import annotations

import math

from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_FOLLOW_TIMEOUT_SECONDS,
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_PAGE_RECORD_LIMIT,
)
from theater.daemon.rpc.router import method
from theater.models import BadRequest


@method("trajectory.snapshot")
async def _trajectory_snapshot(daemon, params: dict) -> dict:
    method_name = "trajectory.snapshot"
    participant_id = _required_string(params, "id", method_name)
    before = _optional_cursor(params, "before", method_name)
    limit = _bounded_limit(params, method_name)
    page = await daemon.trajectory.snapshot(participant_id, before=before, limit=limit)
    return page.to_wire()


@method("trajectory.follow")
async def _trajectory_follow(daemon, params: dict) -> dict:
    method_name = "trajectory.follow"
    participant_id = _required_string(params, "id", method_name)
    stream_id = _required_bounded_string(
        params, "stream_id", method_name, TRAJECTORY_IDENTIFIER_MAX_BYTES
    )
    after = _required_bounded_string(params, "after", method_name, TRAJECTORY_CURSOR_MAX_BYTES)
    wait = _bounded_wait(params, method_name)
    limit = _bounded_limit(params, method_name)
    delta = await daemon.trajectory.follow(
        participant_id,
        stream_id=stream_id,
        after=after,
        wait=wait,
        limit=limit,
    )
    return delta.to_wire()


@method("trajectory.close")
async def _trajectory_close(daemon, params: dict) -> dict:
    method_name = "trajectory.close"
    participant_id = _required_string(params, "id", method_name)
    stream_id = params.get("stream_id")
    if stream_id is not None:
        if not isinstance(stream_id, str) or not stream_id:
            raise BadRequest(
                f"{method_name} parameter 'stream_id' must be a non-empty string or null"
            )
        try:
            encoded_length = len(stream_id.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise BadRequest(
                f"{method_name} parameter 'stream_id' must contain valid UTF-8"
            ) from exc
        if encoded_length > TRAJECTORY_IDENTIFIER_MAX_BYTES:
            raise BadRequest(f"{method_name} parameter 'stream_id' exceeds the identifier limit")
    released = daemon.trajectory.close_viewer(participant_id, stream_id)
    return {"released": released}


@method("trajectory.locate")
async def _trajectory_locate(daemon, params: dict) -> dict:
    method_name = "trajectory.locate"
    participant_id = _required_identifier(params, "id", method_name)
    record_id = _required_identifier(params, "record_id", method_name)
    return daemon.trajectory.locate(participant_id, record_id).to_wire()


def _required_string(params: dict, key: str, method_name: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise BadRequest(f"{method_name} requires non-empty string parameter {key!r}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BadRequest(f"{method_name} parameter {key!r} must contain valid UTF-8") from exc
    return value


def _required_bounded_string(params: dict, key: str, method_name: str, max_bytes: int) -> str:
    value = _required_string(params, key, method_name)
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise BadRequest(f"{method_name} parameter {key!r} must contain valid UTF-8") from exc
    if encoded_length > max_bytes:
        raise BadRequest(f"{method_name} parameter {key!r} exceeds {max_bytes} encoded bytes")
    return value


def _required_identifier(params: dict, key: str, method_name: str) -> str:
    value = _required_bounded_string(params, key, method_name, TRAJECTORY_IDENTIFIER_MAX_BYTES)
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        raise BadRequest(f"{method_name} parameter {key!r} must not contain control characters")
    return value


def _optional_cursor(params: dict, key: str, method_name: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BadRequest(f"{method_name} parameter {key!r} must be a non-empty string or null")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise BadRequest(f"{method_name} parameter {key!r} must contain valid UTF-8") from exc
    if encoded_length > TRAJECTORY_CURSOR_MAX_BYTES:
        raise BadRequest(f"{method_name} parameter {key!r} exceeds the cursor limit")
    return value


def _bounded_limit(params: dict, method_name: str) -> int:
    value = params.get("limit", TRAJECTORY_PAGE_RECORD_LIMIT)
    if type(value) is not int or value <= 0:
        raise BadRequest(f"{method_name} parameter 'limit' must be a positive integer")
    return min(value, TRAJECTORY_PAGE_RECORD_LIMIT)


def _bounded_wait(params: dict, method_name: str) -> float:
    value = params.get("wait", TRAJECTORY_FOLLOW_TIMEOUT_SECONDS)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise BadRequest(f"{method_name} parameter 'wait' must be a non-negative finite number")
    return min(float(value), TRAJECTORY_FOLLOW_TIMEOUT_SECONDS)


__all__ = []
