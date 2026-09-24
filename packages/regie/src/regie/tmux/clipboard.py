"""Copy text to the system clipboard through whatever this machine offers."""

from __future__ import annotations

import asyncio
import contextlib
import shutil

# Tried in order; the first installed one receives the text on stdin.
_COMMANDS = (
    ("pbcopy",),
    ("wl-copy",),
    ("xclip", "-selection", "clipboard"),
    ("xsel", "--clipboard", "--input"),
)


async def copy_to_system_clipboard(text: str) -> bool:
    """Copy with a local clipboard tool; False when none is installed or it failed."""
    for command in _COMMANDS:
        if shutil.which(command[0]) is None:
            continue
        with contextlib.suppress(OSError):
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.communicate(text.encode("utf-8"))
            if process.returncode == 0:
                return True
    return False


__all__ = ["copy_to_system_clipboard"]
