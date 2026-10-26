"""A scripted unix-socket server for driving the public SDK without a daemon."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@asynccontextmanager
async def fixture_server(handler: Handler, *, prefix: str = "sdk-") -> AsyncIterator[Path]:
    """Serve `handler` once per connection; raise the first handler failure on exit."""
    root = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    socket_path = root / "frontend.sock"
    writers: set[asyncio.StreamWriter] = set()
    tasks: set[asyncio.Task[None]] = set()
    failures: list[BaseException] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.add(writer)
        try:
            await handler(reader, writer)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failures.append(exc)
        finally:
            writers.discard(writer)
            tasks.discard(task)
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(serve, path=str(socket_path))
    try:
        yield socket_path
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            for task in tuple(tasks):
                task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        socket_path.unlink(missing_ok=True)
        shutil.rmtree(root)
        if failures:
            raise failures[0]


async def send_frame(writer: asyncio.StreamWriter, value: dict[str, object]) -> None:
    writer.write(json.dumps(value, allow_nan=False, separators=(",", ":")).encode() + b"\n")
    await writer.drain()
