"""Tmux paste-buffer operations that do not target participant panes."""


async def set_buffer(text: str) -> None:
    """Replace the default tmux buffer with literal text."""
    from theater.tmux.client import run

    await run("set-buffer", "--", text)


__all__ = ["set_buffer"]
