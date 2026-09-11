"""Explicit private socket routing and owned PTY clients for presence tests."""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import signal
from pathlib import Path


class PrivateServer:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.socket = str(self.root / "server.sock")
        self.waiters: list[asyncio.subprocess.Process] = []

    def argv(self, args):
        if str(args[0]) != "tmux":
            return args
        if "-S" in args:
            socket = Path(args[args.index("-S") + 1]).resolve()
            assert socket.parent == self.root, "test attempted a non-private tmux socket"
            return args
        assert "-L" not in args, "test must use an explicit private socket"
        return ["tmux", "-S", self.socket, *args[1:]]

    async def command(self, *args):
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "-S",
            self.socket,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 10)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise
        assert proc.returncode == 0, err.decode()
        return out.decode().strip()


class AttachedClient:
    """One owned tmux terminal client, always attached to an explicit socket."""

    def __init__(self, pid, fd):
        self.pid, self.fd = pid, fd

    @classmethod
    def spawn(cls, server: PrivateServer, session: str, *flags) -> AttachedClient:
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(
                "tmux",
                ["tmux", "-S", server.socket, "attach", *flags, "-t", session],
                dict(os.environ, TERM="xterm-256color"),
            )
            os._exit(1)
        return cls(pid, fd)

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def close(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(self.pid, 0)
        os.close(self.fd)
