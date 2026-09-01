"""Tmux paste-buffer operations that do not target participant panes."""


async def set_buffer(text: str) -> None:
    """Replace the default tmux buffer and send literal text to the terminal clipboard."""
    from theater.tmux.client import run

    await run("set-buffer", "-w", "--", text)


__all__ = ["set_buffer"]
