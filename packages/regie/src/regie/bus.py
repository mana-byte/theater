"""Independent bounded diagnostic-bus reads for the optional Régie panel."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from theater.frontend import FrontendClient


class DiagnosticBusController:
    """Keep diagnostic-bus cursors separate from orchestration state following."""

    def __init__(self, client: FrontendClient, *, batch: int) -> None:
        self._client = client
        self._batch = batch
        self._after_id = 0
        self._last_gap = 0
        self._lock = asyncio.Lock()

    @property
    def after_id(self) -> int:
        return self._after_id

    @property
    def last_gap(self) -> int:
        return self._last_gap

    async def poll(self) -> tuple[Mapping[str, object], ...]:
        async with self._lock:
            response = await self._client.diagnostics.bus_tail(
                after_id=self._after_id,
                limit=self._batch,
            )
            items = getattr(getattr(response, "value", None), "items", None)
            if not isinstance(items, tuple | list):
                raise TypeError("diagnostic bus response must contain an item sequence")
            rows: list[Mapping[str, object]] = []
            for item in items:
                if not isinstance(item, Mapping):
                    raise TypeError("diagnostic bus items must be mappings")
                rows.append(dict(item))
            rows.sort(key=_event_id)
            first_id = next(
                (identifier for row in rows if type(identifier := row.get("id")) is int),
                None,
            )
            self._last_gap = (
                max(0, first_id - self._after_id - 1)
                if isinstance(first_id, int) and self._after_id > 0
                else 0
            )
            for event in rows:
                identifier = event.get("id")
                if type(identifier) is int and identifier > self._after_id:
                    self._after_id = identifier
            return tuple(rows)


def _event_id(row: Mapping[str, object]) -> int:
    identifier = row.get("id")
    return identifier if type(identifier) is int else -1


__all__ = ["DiagnosticBusController"]
