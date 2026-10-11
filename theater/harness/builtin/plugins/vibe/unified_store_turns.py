"""Turn lifecycle replay over a Unified store's journal records and retained segments."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .unified_store_io import _reject_symlink_components
from .unified_store_loading import (
    _enumerate_generations,
    _load_generation_manifest,
    _read_journal_detail,
    _session_root,
)
from .unified_store_types import UnifiedStoreError, UnifiedStoreView, _JournalRecord, _Manifest

logger = logging.getLogger("theater.observer")

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
        elif record.record_type == "projection_delta":
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

    None when the retained generations' journals do not cover that range contiguously.
    """
    if through <= after:
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
    for index, manifest in enumerate(segments):
        following = segments[index + 1].first_sequence if index + 1 < len(segments) else None
        last = through if following is None else min(through, following - 1)
        if last < expected:
            continue
        if manifest.first_sequence > expected:
            return None  # the segment holding the next record is no longer retained
        path = session_root / manifest.journal_path
        _reject_symlink_components(session_root, path)
        for record in _read_journal_detail(path, manifest.first_sequence).records:
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
    return retained_turn_changes(
        session_root,
        after=previous.sequence,
        through=current.sequence,
        since=latest_turn(previous.snapshot),
    )


def _segments(session_root: Path) -> list[_Manifest]:
    """Retained generations' journal segments in sequence order; unreadable ones are skipped."""
    manifests: dict[str, _Manifest] = {}
    for generation in _enumerate_generations(session_root):
        try:
            manifest = _load_generation_manifest(
                session_root / "generations" / generation,
                session_root.name,
                generation,
                manifest_sha256=None,
                expected_snapshot_sequence=None,
            )
        except (OSError, UnifiedStoreError, ValueError):
            logger.debug("skipping unreadable Vibe generation %s", generation, exc_info=True)
            continue
        manifests.setdefault(manifest.journal_path, manifest)
    return sorted(manifests.values(), key=lambda manifest: manifest.first_sequence)
