"""Tmux paste-buffer operations that do not target participant panes."""


async def set_buffer(text: str) -> None:
    """Replace the default tmux buffer and send literal text to the terminal clipboard."""
    from theater.tmux.client import run

    # tmux command arguments share a small command-IPC limit. Feed clipboard
    # content over stdin so a bounded trajectory detail cannot exceed it.
    await run("load-buffer", "-w", "-", input_text=text)


__all__ = ["set_buffer"]
