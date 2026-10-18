"""A lane the daemon hung up on must not be reused as if still open."""

import asyncio
import tempfile
from pathlib import Path

from theater.frontend.transport import FrontendTransport


async def test_transport_is_disconnected_once_the_peer_hangs_up():
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        path = Path(directory) / "daemon.sock"

        async def hang_up(_reader, writer):
            writer.close()

        server = await asyncio.start_unix_server(hang_up, path)
        transport = FrontendTransport(path)
        try:
            await transport.connect()
            for _ in range(100):
                if not transport.connected:
                    break
                await asyncio.sleep(0.01)
            assert not transport.connected
        finally:
            transport.abort()
            server.close()
            await server.wait_closed()
