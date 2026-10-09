"""Restore in-memory observation state when durable application aborts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

_JOURNALED = ("say", "hear", "take", "mark_handled")


def _journal_turns(turns, undo: list[Callable[[], None]]) -> None:
    """Shadow the mutators on ``turns`` so each call records only what it changes."""
    original = {name: getattr(turns, name) for name in _JOURNALED}

    def say(text, raw_text=None):
        blocks, raw = len(turns._blocks), len(turns._raw_blocks)
        original["say"](text, raw_text)

        def restore() -> None:
            del turns._blocks[blocks:]
            del turns._raw_blocks[raw:]

        undo.append(restore)

    def hear(text):
        heard = len(turns._heard)
        original["hear"](text)

        def restore() -> None:
            del turns._heard[heard:]

        undo.append(restore)

    def take():
        saved = (list(turns._blocks), list(turns._raw_blocks), list(turns._heard))

        def restore() -> None:
            for target, items in zip(
                (turns._blocks, turns._raw_blocks, turns._heard), saved, strict=True
            ):
                target[:] = items

        undo.append(restore)
        return original["take"]()

    def mark_handled(turn_id):
        if turn_id is not None and turn_id not in turns._seen:
            answered = list(turns._answered)  # bounded by ANSWERED_TURNS

            def restore() -> None:
                turns._answered.clear()
                turns._answered.extend(answered)
                turns._seen.clear()
                turns._seen.update(answered)

            undo.append(restore)
        original["mark_handled"](turn_id)

    for wrapper in (say, hear, take, mark_handled):
        setattr(turns, wrapper.__name__, wrapper)


@contextmanager
def rollback_observation_state(clock, turns) -> Iterator[None]:
    """Undo this unit's own mutations if it fails; cost tracks the write set, not history."""
    saved_clock = dict(clock.__dict__)
    prior = {name: turns.__dict__.get(name) for name in _JOURNALED}
    undo: list[Callable[[], None]] = []
    _journal_turns(turns, undo)
    try:
        yield
    except BaseException:
        clock.__dict__.update(saved_clock)
        for step in reversed(undo):
            step()
        raise
    finally:
        for name, shadow in prior.items():
            if shadow is None:
                turns.__dict__.pop(name, None)
            else:
                turns.__dict__[name] = shadow
