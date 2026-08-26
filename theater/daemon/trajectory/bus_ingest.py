"""Theater-bus ingestion for warm trajectory streams."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterable

from theater.constants.daemon import BUS_PARTICIPANT_PAGE_MAX_LIMIT
from theater.daemon.trajectory.stream import TrajectoryStream
from theater.daemon.trajectory.theater_events import ALLOWLISTED_BUS_KINDS, project_bus_row
from theater.trajectory import TrajectoryRecord

logger = logging.getLogger("theater.daemon.trajectory")


def merge_bus_rows(
    stream: TrajectoryStream,
    rows: Iterable[dict],
    *,
    notify: bool,
    merge_records: Callable[..., object],
) -> bool:
    values = tuple(rows)
    records = project_bus_rows(values, stream.participant.id)
    merge_records(stream, records, notify=notify)
    update_theater_floor(stream, records)
    return any(row.get("kind") == "participant.dead" for row in values)


def project_bus_rows(rows: Iterable[dict], participant_id: str) -> tuple[TrajectoryRecord, ...]:
    return tuple(
        record for row in rows if (record := project_bus_row(row, participant_id)) is not None
    )


def bus_history(
    store,
    participant_id: str,
    *,
    before_id: int | None = None,
    stream: TrajectoryStream | None = None,
    add_gap: Callable[..., bool] | None = None,
) -> list[dict]:
    try:
        return store.bus_page_for_participant(
            participant_id,
            before_id=before_id,
            limit=BUS_PARTICIPANT_PAGE_MAX_LIMIT,
            kinds=ALLOWLISTED_BUS_KINDS,
        )
    except Exception as exc:
        logger.debug("trajectory bus history unavailable: %s", exc)
        if stream is not None and add_gap is not None:
            add_gap(stream, "theater", f"theater bus history unavailable: {exc}")
        return []


def update_theater_floor(stream: TrajectoryStream, records: Iterable[TrajectoryRecord]) -> None:
    values = tuple(records)
    if not values:
        return
    floor = min(record.raw_index for record in values)
    if stream.theater_floor is not None and stream.theater_floor.startswith("bus:"):
        with contextlib.suppress(ValueError):
            floor = min(floor, int(stream.theater_floor.removeprefix("bus:")))
    stream.theater_floor = f"bus:{floor}"


__all__ = ["bus_history", "merge_bus_rows", "project_bus_rows", "update_theater_floor"]
