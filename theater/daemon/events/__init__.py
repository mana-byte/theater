"""Read-only orchestration-state snapshot and follow composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from theater.daemon.events.follow import FollowService
from theater.daemon.events.snapshot import SnapshotService
from theater.models import new_id, now


class StateService:
    """One daemon-owned state reader sharing a durable journal and snapshot cache."""

    def __init__(
        self,
        store,
        *,
        clock: Callable[[], float] = now,
        id_factory: Callable[[], str] = new_id,
    ) -> None:
        self.snapshots = SnapshotService(store, clock=clock, id_factory=id_factory)
        self.follows = FollowService(store.journal)

    def snapshot(self, actor_client_id: str, *, page_size: int) -> dict[str, object]:
        return self.snapshots.snapshot(actor_client_id, page_size=page_size)

    def page(self, actor_client_id: str, snapshot_id: str, page: int) -> dict[str, object]:
        return self.snapshots.page(actor_client_id, snapshot_id, page)

    def release(self, actor_client_id: str, snapshot_id: str) -> None:
        self.snapshots.release(actor_client_id, snapshot_id)

    async def follow(
        self,
        cursor: Mapping[str, object],
        *,
        wait_seconds: object,
        limit: int,
    ) -> dict[str, object]:
        return await self.follows.follow(cursor, wait_seconds=wait_seconds, limit=limit)

    async def aclose(self) -> None:
        await self.follows.aclose()


__all__ = ["StateService"]
