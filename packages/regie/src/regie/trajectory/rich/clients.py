"""Typed trajectory operations and compatibility for older controller clients."""

from __future__ import annotations

import inspect
from typing import Protocol, cast

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


class TrajectoryQueryClient(Protocol):
    async def snapshot(
        self, participant_id: str, *, limit: int, before: str | None = None
    ) -> TrajectoryPage: ...

    async def locate(self, participant_id: str, record_id: str) -> TrajectoryLocation: ...

    async def search(
        self, participant_id: str, query: str, *, limit: int
    ) -> TrajectorySearchResult: ...

    async def release(self, participant_id: str, stream_id: str | None = None) -> None: ...


class TrajectoryFollowClient(Protocol):
    async def follow(
        self, participant_id: str, stream_id: str, after: str, *, wait: float, limit: int
    ) -> TrajectoryDelta: ...


class TypedTrajectoryQuery:
    """Nominal opt-in for clients implementing the typed query protocol."""


class TypedTrajectoryFollow:
    """Nominal opt-in for clients implementing the typed follow protocol."""


async def _invoke(client: object, method: str, **params: object) -> object:
    call = getattr(client, "call", None)
    if callable(call):
        result = call(method, **params)
    else:
        operation = getattr(client, method.replace(".", "_"), None)
        if not callable(operation):
            raise TypeError(f"trajectory client does not implement {method}")
        result = operation(**params)
    return await result if inspect.isawaitable(result) else result


class LegacyQueryClient:
    """Decode generic-call and named-operation clients at the compatibility edge."""

    def __init__(self, client: object) -> None:
        self.client = client

    async def snapshot(
        self, participant_id: str, *, limit: int, before: str | None = None
    ) -> TrajectoryPage:
        params: dict[str, object] = {"id": participant_id, "limit": limit}
        if before is not None:
            params["before"] = before
        return decode_page(await _invoke(self.client, "trajectory.snapshot", **params))

    async def locate(self, participant_id: str, record_id: str) -> TrajectoryLocation:
        return decode_location(
            await _invoke(self.client, "trajectory.locate", id=participant_id, record_id=record_id)
        )

    async def search(
        self, participant_id: str, query: str, *, limit: int
    ) -> TrajectorySearchResult:
        return decode_search_result(
            await _invoke(
                self.client, "trajectory.search", id=participant_id, query=query, limit=limit
            )
        )

    async def release(self, participant_id: str, stream_id: str | None = None) -> None:
        params = {"id": participant_id}
        if stream_id is not None:
            params["stream_id"] = stream_id
        await _invoke(self.client, "trajectory.close", **params)


class LegacyFollowClient:
    def __init__(self, client: object) -> None:
        self.client = client

    async def follow(
        self, participant_id: str, stream_id: str, after: str, *, wait: float, limit: int
    ) -> TrajectoryDelta:
        return decode_delta(
            await _invoke(
                self.client,
                "trajectory.follow",
                id=participant_id,
                stream_id=stream_id,
                after=after,
                wait=wait,
                limit=limit,
            )
        )


def query_client(client: object) -> TrajectoryQueryClient:
    if TypedTrajectoryQuery in type(client).__mro__:
        return cast(TrajectoryQueryClient, client)
    return LegacyQueryClient(client)


def follow_client(client: object) -> TrajectoryFollowClient:
    if TypedTrajectoryFollow in type(client).__mro__:
        return cast(TrajectoryFollowClient, client)
    return LegacyFollowClient(client)
