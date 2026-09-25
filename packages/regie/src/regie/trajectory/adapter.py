"""Public frontend adapters for the preserved trajectory controller."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from theater.frontend import FrontendClient


class TrajectoryQueryAdapter:
    """Translate the rich controller's canonical calls to the public SDK."""

    def __init__(self, client: FrontendClient) -> None:
        self._client = client
        self._request_lock = asyncio.Lock()

    async def call(self, method: str, **params: object) -> object:
        """Serialize requests sharing the SDK's one interactive connection."""
        async with self._request_lock:
            return await self._call(method, **params)

    async def _call(self, method: str, **params: object) -> object:
        participant_id = _participant_id(params)
        if method == "trajectory.snapshot":
            snapshot_result = await self._client.trajectory.snapshot(
                participant_id,
                **_optional(params, "before", "limit"),
            )
            return _plain_json(snapshot_result.value)
        if method == "trajectory.locate":
            record_id = _required_string(params, "record_id")
            return _plain_json(
                (await self._client.trajectory.locate(participant_id, record_id)).value
            )
        if method == "trajectory.search":
            query = _required_string(params, "query")
            search_result = await self._client.trajectory.search(
                participant_id,
                query,
                **_optional(params, "limit"),
            )
            page = search_result.value
            return _plain_json(
                {
                    **dict(page.extra),
                    "records": list(page.items),
                }
            )
        if method == "trajectory.close":
            stream_id = params.get("stream_id")
            if stream_id is None:
                return {"released": False}
            if not isinstance(stream_id, str) or not stream_id:
                raise TypeError("trajectory stream_id must be a non-empty string")
            return (await self._client.trajectory.close(stream_id)).value
        raise TypeError(f"unsupported trajectory query method {method!r}")

    async def close(self) -> None:
        """The owning Régie app closes the shared frontend client."""


class TrajectoryFollowAdapter:
    """Route long polls through the SDK's dedicated trajectory lane."""

    def __init__(self, client: FrontendClient) -> None:
        self._client = client

    async def call(self, method: str, **params: object) -> object:
        if method != "trajectory.follow":
            raise TypeError(f"unsupported trajectory follow method {method!r}")
        stream_id = _required_string(params, "stream_id")
        cursor = _required_string(params, "after")
        result = await self._client.trajectory.follow(
            stream_id,
            cursor,
            **_renamed_optional(params, {"wait": "wait_seconds"}),
        )
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
