"""Small asynchronous tmux command boundary owned by Régie."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import Mapping, Sequence

RUN_TIMEOUT_SECONDS = 10.0


class TmuxError(RuntimeError):
    """A tmux command or identity check failed."""


class TmuxMissing(TmuxError):
    """The tmux executable is unavailable."""


def available() -> bool:
    return shutil.which("tmux") is not None


async def run(
    *args: str,
    check: bool = True,
    input_bytes: bytes | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Run tmux without a shell and return decoded standard output."""
    if not available():
        raise TmuxMissing("tmux is not on PATH")
    process = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=None if environment is None else dict(environment),
    )
    try:
        output, error = await asyncio.wait_for(
            process.communicate(input_bytes), timeout=RUN_TIMEOUT_SECONDS
        )
    except (TimeoutError, asyncio.CancelledError) as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise TmuxError(f"tmux {args[0] if args else ''} timed out") from None
    if check and process.returncode != 0:
        detail = error.decode("utf-8", "backslashreplace").strip()
        raise TmuxError(f"tmux {args[0] if args else ''} failed: {detail}")
    return output.decode("utf-8", "backslashreplace").rstrip("\n")


async def run_command(
    args: Sequence[str],
    *,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> str:
    return await run(*args, check=check, input_bytes=input_bytes)


__all__ = ["TmuxError", "TmuxMissing", "available", "run", "run_command"]
