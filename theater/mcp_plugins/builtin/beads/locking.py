"""Process-shared board serialization and cancellation drainage."""

from __future__ import annotations

import asyncio
import fcntl
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any


@asynccontextmanager
async def board_lock(state_dir: Path, instance_id: str):
    lock_dir = state_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{instance_id}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


async def drain_cancelled_worker(worker: asyncio.Task[Any]) -> None:
    while not worker.done():
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(worker)
    with suppress(asyncio.CancelledError):
        worker.exception()
