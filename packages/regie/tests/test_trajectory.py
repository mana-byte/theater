from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from regie.trajectory import TrajectoryController

from theater.frontend import FrontendClient


class Trajectory:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def snapshot(self, participant_id: str, *, limit: int) -> object:
        self.calls.append(("snapshot", participant_id, limit))
        return SimpleNamespace(
            value={
                "stream_id": "trajectory-a",
                "cursor": "cursor-a",
                "records": [
                    {
                        "record_id": "record-a",
                        "participant_id": participant_id,
                        "revision": 1,
                    }
                ],
            }
        )

    async def follow(self, stream_id: str, cursor: str | None, *, wait_seconds: int) -> object:
        self.calls.append(("follow", stream_id, cursor, wait_seconds))
        return SimpleNamespace(
            value={
                "stream_id": "trajectory-a",
                "cursor": "cursor-b",
                "upserts": [
                    {
                        "record": {
                            "record_id": "record-a",
                            "participant_id": "participant-a",
                            "revision": 2,
                            "future": True,
                        }
                    }
                ],
            }
        )

    async def close(self, stream_id: str) -> object:
        self.calls.append(("close", stream_id))
        return SimpleNamespace(value={"released": True})


class Client:
    def __init__(self) -> None:
        self.trajectory = Trajectory()


@pytest.mark.asyncio
async def test_trajectory_uses_its_own_public_cursor_lifecycle() -> None:
    client = Client()
    controller = TrajectoryController(cast(FrontendClient, client), page_size=30)

    opened = await controller.open("participant-a")
    followed = await controller.follow_once()

    assert opened.stream_id == "trajectory-a"
    assert opened.cursor == "cursor-a"
    assert followed is not None and followed["cursor"] == "cursor-b"
    assert controller.state is not None
    assert controller.state.records["record-a"]["future"] is True
    await controller.close()
    assert client.trajectory.calls == [
        ("snapshot", "participant-a", 30),
        ("follow", "trajectory-a", "cursor-a", 0),
        ("close", "trajectory-a"),
    ]


@pytest.mark.asyncio
async def test_trajectory_resync_replaces_the_viewer_from_its_own_public_snapshot() -> None:
    class ResyncTrajectory(Trajectory):
        def __init__(self) -> None:
            super().__init__()
            self.snapshots = 0

        async def snapshot(self, participant_id: str, *, limit: int) -> object:
            self.snapshots += 1
            self.calls.append(("snapshot", participant_id, limit))
            return SimpleNamespace(
                value={
                    "stream_id": "trajectory-a",
                    "cursor": f"cursor-{self.snapshots}",
                    "records": [
                        {
                            "record_id": f"record-{self.snapshots}",
                            "participant_id": participant_id,
                            "revision": 1,
                        }
                    ],
                }
            )

        async def follow(self, stream_id: str, cursor: str | None, *, wait_seconds: int) -> object:
            self.calls.append(("follow", stream_id, cursor, wait_seconds))
            return SimpleNamespace(
                value={
                    "stream_id": stream_id,
                    "resync_required": True,
                    "reason": "stream expired",
                }
            )

    client = Client()
    client.trajectory = ResyncTrajectory()
    controller = TrajectoryController(cast(FrontendClient, client), page_size=30)

    await controller.open("participant-a")
    await controller.follow_once()

    assert controller.state is not None
    assert controller.state.cursor == "cursor-2"
    assert tuple(controller.state.records) == ("record-2",)
