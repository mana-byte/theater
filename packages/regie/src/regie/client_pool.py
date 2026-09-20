"""Independent public-SDK clients for Régie's concurrent frontend work."""

from __future__ import annotations

import contextlib
from itertools import count

from theater.frontend import FrontendClient

_LONG_POLL_REQUEST_TIMEOUT_SECONDS = 35.0


class FrontendClientPool:
    """Clone real SDK clients while keeping lightweight test doubles usable."""

    _PURPOSES = (
        "state",
        "catalog",
        "usage",
        "bus",
        "animation_bus",
        "trajectory_query",
        "trajectory_follow",
        "transcripts",
        "controls",
        "resume",
    )

    def __init__(self, client: FrontendClient) -> None:
        self._root = client
        self._action_ids = count(1)
        self._transcript_ids = count(1)
        if isinstance(client, FrontendClient):
            self._clients = {
                purpose: _clone_client(
                    client,
                    purpose,
                    minimum_timeout=(
                        _LONG_POLL_REQUEST_TIMEOUT_SECONDS
                        if purpose in {"trajectory_follow", "transcripts"}
                        else None
                    ),
                )
                for purpose in self._PURPOSES
            }
        else:
            self._clients = dict.fromkeys(self._PURPOSES, client)

    def __getattr__(self, purpose: str) -> FrontendClient:
        try:
            return self._clients[purpose]
        except KeyError as exc:
            raise AttributeError(purpose) from exc

    def action_client(self) -> FrontendClient:
        """Return a client whose ordinary and operation-wait lanes belong to one action."""
        if not isinstance(self._root, FrontendClient):
            return self._root
        return _clone_client(
            self._root,
            f"action-{next(self._action_ids)}",
            minimum_timeout=_LONG_POLL_REQUEST_TIMEOUT_SECONDS,
        )

    def transcript_client(self) -> FrontendClient:
        """Return a client dedicated to one retry-safe transcript bind."""
        if not isinstance(self._root, FrontendClient):
            return self._root
        return _clone_client(self._root, f"transcript-{next(self._transcript_ids)}")

    async def close(self) -> None:
        clients = {id(client): client for client in (self._root, *self._clients.values())}
        for client in clients.values():
            with contextlib.suppress(Exception):
                await client.close()


def _clone_client(
    client: FrontendClient,
    purpose: str,
    *,
    minimum_timeout: float | None = None,
) -> FrontendClient:
    config = client.config
    timeout = config.request_timeout
    if timeout is not None and minimum_timeout is not None:
        timeout = max(timeout, minimum_timeout)
    return FrontendClient(
        config.socket_path,
        client_id=f"{config.client_id}:{purpose}",
        role=config.role,
        channel=config.channel,
        required_capabilities=config.required_capabilities,
        provider_id=config.provider_id,
        provider_credential=config.provider_credential,
        request_timeout=timeout,
    )


__all__ = ["FrontendClientPool"]
