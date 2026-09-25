"""Bus audit rows and synchronous post-commit bus listeners."""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from contextlib import suppress
from copy import deepcopy

from theater.constants.daemon import BUS_PARTICIPANT_PAGE_MAX_LIMIT
from theater.daemon.persistence.store_parts._host import BusListener, StoreHost
from theater.models import now

logger = logging.getLogger("theater.store")


class BusStore(StoreHost):
    """Store-facing bus methods; state lives on ``Store``."""

    def register_bus_listener(self, listener: BusListener) -> None:
        """Register one synchronous best-effort post-commit bus listener."""
        if listener not in self._bus_listeners:
            self._bus_listeners.append(listener)

    def unregister_bus_listener(self, listener: BusListener) -> None:
        """Remove a bus listener; repeated removal is harmless."""
        with suppress(ValueError):
            self._bus_listeners.remove(listener)

    @staticmethod
    def _bus_row(
        row_id: int,
        timestamp: float,
        from_id: str | None,
        to_id: str | None,
        kind: str,
        payload_text: str | None,
    ) -> dict:
        return {
            "id": row_id,
            "ts": timestamp,
            "from_id": from_id,
            "to_id": to_id,
            "kind": kind,
            "payload": json.loads(payload_text) if payload_text else None,
        }

    def _notify_bus_listeners(self, rows: list[dict], listeners: tuple[BusListener, ...]) -> None:
        """Notify listeners after commit without letting one failure escape."""
        for row in rows:
            for listener in listeners:
                try:
                    listener(deepcopy(row))
                except Exception:
                    logger.exception("bus listener failed for row %s", row.get("id"))

    def bus_append(
        self,
        kind: str,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        payload: dict | None = None,
    ) -> int:
        listeners = tuple(self._bus_listeners)
        timestamp = now() if listeners else None
        row_id = self._bus.append(
            kind,
            from_id=from_id,
            to_id=to_id,
            payload=payload,
            timestamp=timestamp,
        )
        if listeners:
            assert timestamp is not None
            payload_text = json.dumps(payload) if payload else None
            row = self._bus_row(row_id, timestamp, from_id, to_id, kind, payload_text)
            self._notify_bus_listeners([row], listeners)
        return row_id

    def bus_page_for_participant(
        self,
        participant_id: str,
        *,
        before_id: int | str | None = None,
        limit: int = BUS_PARTICIPANT_PAGE_MAX_LIMIT,
        kinds: Collection[str],
    ) -> list[dict]:
        return self._bus.page_for_participant(
            participant_id,
            before_id=before_id,
            limit=limit,
            kinds=kinds,
        )

    def bus_record_for_participant(
        self,
        participant_id: str,
        row_id: int,
        *,
        kinds: Collection[str],
    ) -> dict | None:
        return self._bus.record_for_participant(participant_id, row_id, kinds=kinds)

    def bus_tail(self, limit: int = 100, *, after_id: int = 0) -> list[dict]:
        return self._bus.tail(limit, after_id=after_id)

    def observation_error_active(self, participant_id: str, code: str) -> bool:
        """Whether an observation error remains uncleared in the audit stream."""
        return self._bus.observation_error_active(participant_id, code)

    def observation_error_timestamp(self, participant_id: str, code: str) -> float | None:
        """The wall-clock ``ts`` of the most recent uncleared observation error."""
        return self._bus.observation_error_timestamp(participant_id, code)
