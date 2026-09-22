"""Migration guidance for the former bare Régie launcher."""

from __future__ import annotations


def cmd_launch(args) -> int:
    """Point a direct caller at the independent Régie executable."""
    del args
    print("Régie is now the standalone `regie` command. Run `regie` to start the UI.")
    return 0
