"""Transcript-history ingestion for warm trajectory streams."""

from __future__ import annotations

import logging
from collections.abc import Callable

from theater.daemon.trajectory.history import HistoryLoad, source_epoch_for
from theater.daemon.trajectory.observed_timing import (
    apply_observation_points,
    observation_points_for_history,
)
from theater.daemon.trajectory.project import project_history_page
from theater.daemon.trajectory.stream import TrajectoryStream
from theater.provenance import is_trusted_provenance
from theater.trajectory import PanelState, TrajectoryRecord

logger = logging.getLogger("theater.daemon.trajectory")


def apply_history(
    stream: TrajectoryStream,
    result: HistoryLoad,
    *,
    older: bool,
    store,
    merge_records: Callable[..., object],
    add_gap: Callable[..., bool],
    add_boundary: Callable[..., object],
) -> bool:
    page = result.page
    if not result.trusted:
        reason = result.message or page.error or "history source failed"
        return add_gap(stream, "transcript", reason)
    stream.trusted = True
    stream.live_allowed = is_trusted_provenance(page.provenance) or is_trusted_provenance(
        stream.participant.session_correlation
    )
    if result.source_epoch is not None:
        if stream.source_epoch is not None and stream.source_epoch != result.source_epoch:
            add_boundary(stream, stream.source_epoch, result.source_epoch)
            add_gap(stream, "transcript", "transcript session rotated")
        stream.source_epoch = result.source_epoch
    try:
        stream.observation_points = observation_points_for_history(
            store,
            stream.participant.id,
            page.location,
        )
    except Exception as exc:
        logger.debug("trajectory observation timing unavailable: %s", exc)
        stream.observation_points = ()
    records = project_history_page(
        page,
        participant_id=stream.participant.id,
        source_epoch=stream.source_epoch or source_epoch_for(stream.participant, None),
    )
    records = apply_observation_points(records, stream.observation_points)
    merge_records(stream, records, notify=False)
    if page.older_cursor is not None and page.has_older:
        stream.source_before = page.older_cursor
    elif older:
        stream.source_before = None
    if page.cursor is not None:
        stream.transcript_floor = page.older_cursor or page.cursor
    return False


def apply_older_history(
    stream: TrajectoryStream,
    result: HistoryLoad,
    *,
    source_before: str | None,
    merge_records: Callable[..., object],
    add_gap: Callable[..., bool],
    set_panel: Callable[..., bool],
) -> tuple[str | None, tuple[TrajectoryRecord, ...]]:
    if not result.trusted:
        reason = result.message or result.page.error or "history source failed"
        add_gap(stream, "transcript", reason)
        set_panel(
            stream,
            PanelState.STALE,
            f"older transcript history is unavailable: {reason}; retry this older page",
            notify=False,
        )
        return source_before, ()
    records = project_history_page(
        result.page,
        participant_id=stream.participant.id,
        source_epoch=result.source_epoch
        or stream.source_epoch
        or source_epoch_for(stream.participant, None),
    )
    records = apply_observation_points(records, stream.observation_points)
    merge_records(stream, records, notify=False)
    source_before = result.page.older_cursor if result.page.has_older else None
    stream.transcript_floor = result.page.older_cursor or result.page.cursor
    return source_before, records


__all__ = ["apply_history", "apply_older_history"]
