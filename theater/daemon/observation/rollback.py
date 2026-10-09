"""Restore in-memory observation state when durable application aborts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy


@contextmanager
def rollback_observation_state(clock, turns) -> Iterator[None]:
    """Restore watcher-local state if the surrounding write unit fails."""
    saved_clock = deepcopy(clock)
    saved_turns = deepcopy(turns)
    try:
        yield
    except BaseException:
        clock.__dict__.update(saved_clock.__dict__)
        turns.__dict__.update(saved_turns.__dict__)
        raise
