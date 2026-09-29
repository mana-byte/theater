"""Literal tmux command encoding for transport over standard input."""

from __future__ import annotations

from collections.abc import Sequence

_ESCAPES = str.maketrans(
    {"\\": "\\\\", '"': '\\"', "$": "\\$", "~": "\\~", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
)


def command_source(argv: Sequence[str]) -> bytes:
    """Quote every word for tmux's parser, without shell or variable expansion."""
    if not argv or any("\x00" in value for value in argv):
        raise ValueError("tmux source requires a nonempty command without NUL bytes")
    return (" ".join('"' + value.translate(_ESCAPES) + '"' for value in argv) + "\n").encode()
