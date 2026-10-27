"""Turn lifecycle replay over a Unified store's journal records and retained segments."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .unified_store_io import _reject_symlink_components
from .unified_store_loading import _read_journal_detail, _session_root
from .unified_store_types import (
    _DELTA_RECORD_TYPES,
    _JOURNAL_PATH_PATTERN,
    UnifiedStoreError,
    UnifiedStoreView,
    _JournalRecord,
)

logger = logging.getLogger("theater.harness.vibe")

#: Turn changes kept for one read; more than this cannot be replayed whole and is reported unknown.
TURN_CHANGES_MAX = 1024


def latest_turn(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    value = snapshot.get("latestTurn")
    return value if isinstance(value, dict) else None


def turn_key(turn: dict[str, Any] | None) -> tuple[Any, Any]:
    return (None, None) if turn is None else (turn.get("id"), turn.get("status"))


class TurnTracker:
    """Every ``latestTurn`` value a run of journal records passes through, oldest first."""

    def __init__(self, since: dict[str, Any] | None) -> None:
        self._turn = since
        self._changes: list[dict[str, Any]] = []

    def feed(self, record: _JournalRecord) -> None:
        turn = self._turn
        if record.record_type == "projection_advanced":
            turn = latest_turn(record.snapshot or {})
        elif record.record_type in _DELTA_RECORD_TYPES:
            envelopes = [op for op in record.delta or () if op.get("op") == "set_envelope"]
            if envelopes:
                turn = latest_turn(envelopes[-1]["state"])
        if turn_key(turn) != turn_key(self._turn):
            self._turn = turn
            if turn is not None:
                self._changes.append(turn)

    def result(self) -> tuple[dict[str, Any], ...] | None:
        """The changes, or None when there were too many to keep whole."""
        return tuple(self._changes) if len(self._changes) <= TURN_CHANGES_MAX else None


def retained_turn_changes(
    session_root: Path, *, after: int, through: int, since: dict[str, Any] | None
) -> tuple[dict[str, Any], ...] | None:
    """Turn changes of records ``after`` < sequence <= ``through``, segment by retained segment.

    None when the retained journals do not cover that range contiguously, or
    when ``through`` lies behind ``after``: history that moved backwards replays nothing.
    """
    if through < after:
        return None
    if through == after:
        return ()
    try:
        return _replay_segments(session_root, after=after, through=through, since=since)
    except (OSError, UnifiedStoreError, ValueError):
        return None


def _replay_segments(
    session_root: Path, *, after: int, through: int, since: dict[str, Any] | None
) -> tuple[dict[str, Any], ...] | None:
    segments = _segments(session_root)
    tracker = TurnTracker(since)
    expected = after + 1
    for index, path in enumerate(segments):
        first_sequence = int(path.stem)
        following = int(segments[index + 1].stem) if index + 1 < len(segments) else None
        last = through if following is None else min(through, following - 1)
        if last < expected:
            continue
        if first_sequence > expected:
            return None  # the segment holding the next record is no longer retained
        _reject_symlink_components(session_root, path)
        for record in _read_journal_detail(path, first_sequence).records:
            if record.sequence > last:
                break
            if record.sequence == expected:
                tracker.feed(record)
                expected += 1
        if expected <= last:
            return None  # the segment ends before its successor begins
        if expected > through:
            return tracker.result()
    return None


def turn_changes_between(
    previous: UnifiedStoreView, current: UnifiedStoreView
) -> tuple[dict[str, Any], ...] | None:
    """Turn changes from one view of a session to a later one, read from retained journals."""
    if previous.session_id != current.session_id:
        return None
    try:
        session_root = _session_root(current.current)
    except (OSError, UnifiedStoreError):
        return None
    changes = retained_turn_changes(
        session_root,
        after=previous.sequence,
        through=current.sequence,
        since=latest_turn(previous.snapshot),
    )
    if changes is None and previous.sequence <= current.sequence:
        if _one_turn(previous, current):
            # Vibe keeps two journals and folds on every turn end and reconfigure, so the journal
            # can be collected before this read; one turn id in both views proves no other ran.
            return ()
        logger.warning(
            "vibe %s: turn ends in sequences %d..%d are not replayable; retained journals: %s",
            current.session_id,
            previous.sequence + 1,
            current.sequence,
            _retained(session_root),
        )
    return changes


def _retained(session_root: Path) -> list[int]:
    try:
        return [int(path.stem) for path in _segments(session_root)]
    except (OSError, UnifiedStoreError):
        return []


def _one_turn(previous: UnifiedStoreView, current: UnifiedStoreView) -> bool:
    turn_id = turn_key(latest_turn(previous.snapshot))[0]
    return turn_id is not None and turn_id == turn_key(latest_turn(current.snapshot))[0]


def _segments(session_root: Path) -> list[Path]:
    """Retained journals in sequence order; generation manifests can be collected first."""
    journals = session_root / "journal"
    _reject_symlink_components(session_root, journals)
    return sorted(
        path
        for path in journals.iterdir()
        if _JOURNAL_PATH_PATTERN.fullmatch(f"journal/{path.name}") is not None
    )
