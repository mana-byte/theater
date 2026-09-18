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


@pytest.mark.asyncio
async def test_trajectory_replacement_releases_the_old_stream_only_after_a_valid_snapshot() -> None:
    class ReplacingTrajectory(Trajectory):
        def __init__(self) -> None:
            super().__init__()
            self.responses: list[object] = [
                {
                    "stream_id": "trajectory-a",
                    "cursor": "cursor-a",
                    "records": [{"record_id": "record-a", "participant_id": "participant-a"}],
                },
                ValueError("malformed replacement"),
                {
                    "stream_id": "trajectory-b",
                    "cursor": "cursor-b",
                    "records": [{"record_id": "record-b", "participant_id": "participant-b"}],
                },
            ]

        async def snapshot(self, participant_id: str, *, limit: int) -> object:
            self.calls.append(("snapshot", participant_id, limit))
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return SimpleNamespace(value=response)

    client = Client()
    client.trajectory = ReplacingTrajectory()
    controller = TrajectoryController(cast(FrontendClient, client), page_size=30)

    first = await controller.open("participant-a")
    with pytest.raises(ValueError, match="malformed replacement"):
        await controller.open("participant-b")
    assert controller.state is first
    assert ("close", "trajectory-a") not in client.trajectory.calls

    replacement = await controller.open("participant-b")
    assert replacement.stream_id == "trajectory-b"
    assert controller.state is replacement
    assert client.trajectory.calls.count(("close", "trajectory-a")) == 1

    await controller.close()
    assert client.trajectory.calls.count(("close", "trajectory-a")) == 1
    assert client.trajectory.calls.count(("close", "trajectory-b")) == 1


@pytest.mark.asyncio
async def test_trajectory_replacement_does_not_close_a_reused_stream_id() -> None:
    class SharedStreamTrajectory(Trajectory):
        async def snapshot(self, participant_id: str, *, limit: int) -> object:
            self.calls.append(("snapshot", participant_id, limit))
            return SimpleNamespace(
                value={
                    "stream_id": "trajectory-shared",
                    "cursor": participant_id,
                    "records": [
                        {
                            "record_id": participant_id,
                            "participant_id": participant_id,
                        }
                    ],
                }
            )

    client = Client()
    client.trajectory = SharedStreamTrajectory()
    controller = TrajectoryController(cast(FrontendClient, client), page_size=30)

    await controller.open("participant-a")
    await controller.open("participant-b")
    await controller.close()

    assert client.trajectory.calls.count(("close", "trajectory-shared")) == 1
