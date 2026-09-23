"""Lightweight Python entry point, timed before importing the launcher."""

from collections.abc import Sequence
from time import monotonic


def main(argv: Sequence[str] | None = None) -> int:
    started_at = monotonic()
    from regie.latency import StartupTrace

    trace = StartupTrace(started_at)
    from regie.cli import main as launch

    trace.record("imports.launcher", started_at)
    return launch(argv, startup=trace)
