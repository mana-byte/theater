"""Public frontend adapters for the preserved trajectory controller."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TypedDict, Unpack, cast

from regie.trajectory.rich.clients import TypedTrajectoryFollow, TypedTrajectoryQuery
from regie.trajectory.rich.models import (
    TrajectoryDelta,
    TrajectoryLocation,
    TrajectoryPage,
    TrajectorySearchResult,
    decode_delta,
    decode_location,
    decode_page,
    decode_search_result,
)
from theater.frontend import FrontendClient


class _SnapshotParams(TypedDict, total=False):
    before: str | None
    limit: int


class _SearchParams(TypedDict, total=False):
    limit: int


class _FollowParams(TypedDict, total=False):
    wait_seconds: float


class TrajectoryQueryAdapter(TypedTrajectoryQuery):
    """Decode named queries while serializing the shared SDK connection."""

    def __init__(self, client: FrontendClient) -> None:
        self._client = client
        self._request_lock = asyncio.Lock()

    async def call(self, method: str, **params: object) -> object:
        """Preserve the exported wire-oriented generic-call interface."""
        async with self._request_lock:
            participant_id = _participant_id(params)
            if method == "trajectory.snapshot":
                return await self._snapshot_wire(
                    participant_id, **cast(_SnapshotParams, _optional(params, "before", "limit"))
                )
            if method == "trajectory.locate":
                return await self._locate_wire(
                    participant_id, _required_string(params, "record_id")
                )
            if method == "trajectory.search":
                return await self._search_wire(
                    participant_id,
                    _required_string(params, "query"),
                    **cast(_SearchParams, _optional(params, "limit")),
                )
            if method == "trajectory.close":
                return await self._release_wire(params.get("stream_id"))
            raise TypeError(f"unsupported trajectory query method {method!r}")

    async def snapshot(
        self, participant_id: str, *, limit: int, before: str | None = None
    ) -> TrajectoryPage:
        options: _SnapshotParams = {"limit": limit}
        if before is not None:
            options["before"] = before
        async with self._request_lock:
            wire = await self._snapshot_wire(participant_id, **options)
        return decode_page(wire)

    async def locate(self, participant_id: str, record_id: str) -> TrajectoryLocation:
        async with self._request_lock:
            wire = await self._locate_wire(participant_id, record_id)
        return decode_location(wire)

    async def search(
        self, participant_id: str, query: str, *, limit: int
    ) -> TrajectorySearchResult:
        async with self._request_lock:
            wire = await self._search_wire(participant_id, query, limit=limit)
        return decode_search_result(wire)

    async def release(self, participant_id: str, stream_id: str | None = None) -> None:
        async with self._request_lock:
            _required_string({"id": participant_id}, "id")
            await self._release_wire(stream_id)

    async def _snapshot_wire(
        self, participant_id: str, **options: Unpack[_SnapshotParams]
    ) -> object:
        participant_id = _required_string({"id": participant_id}, "id")
        result = await self._client.trajectory.snapshot(participant_id, **options)
        return _plain_json(result.value)

    async def _locate_wire(self, participant_id: str, record_id: str) -> object:
        participant_id = _required_string({"id": participant_id}, "id")
        record_id = _required_string({"record_id": record_id}, "record_id")
        result = await self._client.trajectory.locate(participant_id, record_id)
        return _plain_json(result.value)

    async def _search_wire(
        self, participant_id: str, query: str, **options: Unpack[_SearchParams]
    ) -> object:
        participant_id = _required_string({"id": participant_id}, "id")
        query = _required_string({"query": query}, "query")
        result = await self._client.trajectory.search(participant_id, query, **options)
        page = result.value
        return _plain_json({**dict(page.extra), "records": list(page.items)})

    async def _release_wire(self, stream_id: object) -> object:
        if stream_id is None:
            return {"released": False}
        if not isinstance(stream_id, str) or not stream_id:
            raise TypeError("trajectory stream_id must be a non-empty string")
        return (await self._client.trajectory.close(stream_id)).value

    async def close(self) -> None:
        """The owning Régie app closes the shared frontend client."""


class TrajectoryFollowAdapter(TypedTrajectoryFollow):
    """Route long polls through the SDK's dedicated trajectory lane."""

    def __init__(self, client: FrontendClient) -> None:
        self._client = client

    async def call(self, method: str, **params: object) -> object:
        if method != "trajectory.follow":
            raise TypeError(f"unsupported trajectory follow method {method!r}")
        stream_id = _required_string(params, "stream_id")
        cursor = _required_string(params, "after")
        return await self._follow_wire(
            stream_id,
            cursor,
            **cast(_FollowParams, _renamed_optional(params, {"wait": "wait_seconds"})),
        )

    async def follow(
        self, participant_id: str, stream_id: str, after: str, *, wait: float, limit: int
    ) -> TrajectoryDelta:
        return decode_delta(await self._follow_wire(stream_id, after, wait_seconds=wait))

    async def _follow_wire(
        self, stream_id: str, cursor: str, **options: Unpack[_FollowParams]
    ) -> object:
        stream_id = _required_string({"stream_id": stream_id}, "stream_id")
        cursor = _required_string({"after": cursor}, "after")
        result = await self._client.trajectory.follow(stream_id, cursor, **options)
        return _plain_json(result.value)

    async def close(self) -> None:
        """The owning Régie app closes the shared frontend client."""


def _participant_id(params: Mapping[str, object]) -> str:
    return _required_string(params, "id")


def _required_string(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"trajectory {name} must be a non-empty string")
    return value


def _optional(params: Mapping[str, object], *names: str) -> dict[str, object]:
    return {name: params[name] for name in names if name in params}


def _renamed_optional(params: Mapping[str, object], names: Mapping[str, str]) -> dict[str, object]:
    return {target: params[source] for source, target in names.items() if source in params}


def _plain_json(value: object) -> object:
    """Thaw the SDK's immutable JSON view for the preserved rc9 decoders.

    The rc9 domain predates the immutable SDK facade and strictly requires arrays to
    be lists; adapt here rather than weaken its validation.
    """
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


__all__ = ["TrajectoryFollowAdapter", "TrajectoryQueryAdapter"]
