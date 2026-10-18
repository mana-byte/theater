"""Conservative, plugin-owned board retention."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from contextlib import suppress
from pathlib import Path

from theater.plugin_client import TheaterPluginClient

from .backend import Board
from .locking import board_lock, drain_cancelled_worker


class BoardRetention:
    def __init__(self, state_dir: Path, ttl_seconds: int) -> None:
        self.state_dir = state_dir.resolve()
        self.ttl_seconds = ttl_seconds

    def _marker(self, instance_id: str) -> Path:
        return self.state_dir / "retention" / f"{instance_id}.json"

    @staticmethod
    async def _active(client: TheaterPluginClient, instance_id: str) -> bool:
        value = await client.play_instances_status([instance_id])
        if len(value) != 1 or value[0].get("instance_id") != instance_id:
            raise ValueError("incomplete play liveness response")
        if type(value[0].get("active")) is not bool:
            raise ValueError("invalid play liveness response")
        return value[0]["active"]

    async def sweep(self) -> None:
        root = self.state_dir / "plays"
        if not root.is_dir():
            return
        async with TheaterPluginClient() as client:
            for path in root.iterdir():
                try:
                    if path.is_symlink() or not path.is_dir():
                        continue
                    board = Board(self.state_dir, path.name, None)
                    async with board_lock(self.state_dir, board.instance_id):
                        active = await self._active(client, board.instance_id)
                        marker = self._marker(board.instance_id)
                        if active:
                            marker.unlink(missing_ok=True)
                            continue
                        now = time.time()
                        if marker.is_file():
                            inactive_since = json.loads(marker.read_text())["inactive_since"]
                            if not isinstance(inactive_since, (int, float)) or inactive_since > now:
                                continue
                        else:
                            inactive_since = now
                            marker.parent.mkdir(parents=True, exist_ok=True)
                            marker.write_text(json.dumps({"inactive_since": now}))
                        if now - inactive_since >= self.ttl_seconds:
                            if await self._active(client, board.instance_id):
                                marker.unlink(missing_ok=True)
                                continue
                            worker = asyncio.create_task(
                                asyncio.to_thread(shutil.rmtree, board.path)
                            )
                            try:
                                await asyncio.shield(worker)
                            except asyncio.CancelledError:
                                await drain_cancelled_worker(worker)
                                raise
                            marker.unlink(missing_ok=True)
                except Exception:
                    # A failed liveness check must never delete a board.
                    continue

    async def run(self) -> None:
        with suppress(Exception):
            await self.sweep()
        while True:
            await asyncio.sleep(3600)
            with suppress(Exception):
                await self.sweep()
