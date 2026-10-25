"""Where a spawn spends its time: one log line per step, tied together by the operation id.

Régie already times its own request, observation and render (``action.spawn.*``). This adds what
those cannot see: the daemon's own phases with the time it spent in each, how late Régie learned
of each one, when the participant became routable, and the whole path measured from the human's
Enter. Pure bookkeeping over public state; nothing here reads a terminal or the daemon.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from theater.frontend import Operation, StateProjection

if TYPE_CHECKING:
    from regie.controllers.actions import ActionRecord

logger = logging.getLogger("regie.latency")

#: Past this, a step is logged as a warning: the human waited for it.
_SLOW_MS = 1000.0
_LATE_MS = 250.0

_ACTIONS = frozenset({"spawn", "resume"})


class SpawnTrace:
    """Watch each spawn Régie submitted and log every change on its way to a staged terminal."""

    def __init__(self) -> None:
        self._entered: float | None = None
        #: The moment each spawn started counting: the human's Enter, else the submission.
        self._origin: dict[str, float] = {}
        self._phase: dict[str, tuple[str, str]] = {}
        self._participant: dict[str, tuple[bool, str]] = {}

    def entered(self, now: float) -> None:
        """The human confirmed the directory; the next spawn counts from here."""
        self._entered = now

    def observe(
        self,
        projection: StateProjection,
        records: Iterable[ActionRecord],
        *,
        now: float,
        wall: float,
    ) -> None:
        """Log what this projection says has changed for each spawn in flight or just finished."""
        records = tuple(records)
        live: set[str] = set()
        for record in records:
            if record.action not in _ACTIONS:
                continue
            live.add(record.idempotency_key)
            origin = self._origin_of(record)
            operation = (
                projection.operations.get(record.operation_id) if record.operation_id else None
            )
            if operation is not None:
                self._log_phase(operation, origin, now, wall)
            self._log_participant(projection, record, origin, now)
        for stale in set(self._origin) - live:
            del self._origin[stale]
        keys = {record.operation_id for record in records if record.operation_id}
        self._phase = {key: value for key, value in self._phase.items() if key in keys}
        self._participant = {key: value for key, value in self._participant.items() if key in keys}

    def visible(self, record: ActionRecord, now: float) -> None:
        """The spawned row has been drawn."""
        self._milestone("spawn.visible", record, now)

    def staged(self, record: ActionRecord, now: float) -> None:
        """The spawned terminal has been staged and focused."""
        self._milestone("spawn.staged", record, now)

    def _origin_of(self, record: ActionRecord) -> float:
        key = record.idempotency_key
        if key not in self._origin:
            origin = record.submitted_at
            if self._entered is not None:
                logger.info(
                    "spawn.enter_to_submit %.1fms operation=%s",
                    (record.submitted_at - self._entered) * 1000,
                    record.operation_id,
                )
                origin, self._entered = self._entered, None
            self._origin[key] = origin
        return self._origin[key]

    def _log_phase(self, operation: Operation, origin: float, now: float, wall: float) -> None:
        seen = (operation.phase, operation.state)
        if self._phase.get(operation.operation_id) == seen:
            return
        self._phase[operation.operation_id] = seen
        updated, created = operation.updated_at, operation.created_at
        # The daemon's clock and Régie's are one host's: the gap between when the daemon wrote the
        # phase and when this projection reached the screen is delivery plus Régie's own delay.
        late = None if updated is None else max(0.0, wall - updated) * 1000
        into = None if updated is None or created is None else max(0.0, updated - created) * 1000
        logger.log(
            logging.WARNING if late is not None and late >= _LATE_MS else logging.INFO,
            "spawn.phase %s/%s +%.0fms daemon_at=%s late=%s operation=%s",
            operation.phase,
            operation.state,
            (now - origin) * 1000,
            "?" if into is None else f"{into:.0f}ms",
            "?" if late is None else f"{late:.0f}ms",
            operation.operation_id,
        )

    def _log_participant(
        self, projection: StateProjection, record: ActionRecord, origin: float, now: float
    ) -> None:
        participant = (
            projection.participants.get(record.participant_id) if record.participant_id else None
        )
        if participant is None or record.operation_id is None:
            return
        seen = (participant.terminal_route is not None, participant.status)
        if self._participant.get(record.operation_id) == seen:
            return
        self._participant[record.operation_id] = seen
        logger.info(
            "spawn.participant routed=%s status=%s +%.0fms operation=%s",
            seen[0],
            seen[1],
            (now - origin) * 1000,
            record.operation_id,
        )

    def _milestone(self, name: str, record: ActionRecord, now: float) -> None:
        elapsed = (now - self._origin.get(record.idempotency_key, record.submitted_at)) * 1000
        logger.log(
            logging.WARNING if elapsed >= _SLOW_MS else logging.INFO,
            "%s +%.0fms operation=%s",
            name,
            elapsed,
            record.operation_id,
        )


__all__ = ["SpawnTrace"]
