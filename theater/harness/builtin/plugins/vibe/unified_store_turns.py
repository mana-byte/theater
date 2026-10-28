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
    _TURN_STATUSES,
    UnifiedStoreError,
    UnifiedStoreView,
    _JournalRecord,
)

logger = logging.getLogger("theater.harness.vibe")

#: Turn statuses that reached an end; ``in_progress`` is the only one that has not.
_ENDED_TURNS = _TURN_STATUSES - {"in_progress"}

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
    if previous.session_id != current.session_id or previous.sequence > current.sequence:
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
    if changes is None:
        previous_turn = latest_turn(previous.snapshot)
        current_turn = latest_turn(current.snapshot)
        turn_id = turn_key(previous_turn)[0]
        # Vibe folds after every turn; one shared turn id in both views proves no other ran.
        if turn_id is not None and turn_id == turn_key(current_turn)[0]:
            return ()
        if _sole_later_turn_proven(previous, current):
            return ()
        try:
            retained = [int(path.stem) for path in _segments(session_root)]
        except (OSError, UnifiedStoreError):
            retained = []
        logger.warning(
            "vibe %s: sequences %d..%d not replayable; retained journals %s; turns %s/%s -> %s/%s",
            current.session_id,
            previous.sequence + 1,
            current.sequence,
            retained,
            *turn_key(previous_turn),
            *turn_key(current_turn),
        )
    return changes


def _sole_later_turn_proven(previous: UnifiedStoreView, current: UnifiedStoreView) -> bool:
    """Prove from the two views alone that no turn but the later one ran in the gap.

    History cannot rule out a turn that typed no entry: only injected input starts
    one (Vibe's /retry), and it skips the user entry. The watermark can: it advances
    once per journaled publish, and every turn, injected or not, spans at least two
    publishes — its start input, then the input that carries its terminal
    observation — so a gap of at most three publishes fits no second turn. With no
    other turn possible, the later turn's own entries must be the only history
    change and its input entry new."""

    from .unified_history import _entries

    previous_id, previous_status = turn_key(latest_turn(previous.snapshot))
    later = latest_turn(current.snapshot)
    later_id, later_status = turn_key(later)
    if (
        previous_id is None
        or later is None
        or later_id is None
        or previous_id == later_id
        or previous_status not in _ENDED_TURNS
        or later_status not in _ENDED_TURNS
    ):
        return False
    if not 0 < current.watermark - previous.watermark <= 3:
        return False  # too many publishes: an invisible injected turn could fit in the gap
    old_entries = _entries(previous)
    new_entries = _entries(current)
    if new_entries[: len(old_entries)] != old_entries:
        return False  # history moved backwards, lost or altered: another turn could hide there
    appended = new_entries[len(old_entries) :]
    if any(entry.get("turnId") != later_id for entry in appended):
        return False
    input_entry_id = later.get("inputEntryId")
    return (
        isinstance(input_entry_id, str)
        and not any(entry.get("id") == input_entry_id for entry in old_entries)
        and any(
            entry.get("id") == input_entry_id
            and entry.get("type") == "message"
            and entry.get("role") == "user"
            for entry in appended
        )
    )


def _segments(session_root: Path) -> list[Path]:
    """Retained journals in sequence order; generation manifests can be collected first."""
    journals = session_root / "journal"
    _reject_symlink_components(session_root, journals)
    return sorted(
        path
        for path in journals.iterdir()
        if _JOURNAL_PATH_PATTERN.fullmatch(f"journal/{path.name}") is not None
    )
