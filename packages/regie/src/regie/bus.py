"""Independent bounded diagnostic-bus reads for the optional Régie panel."""

from __future__ import annotations

from collections.abc import Mapping

from theater.frontend import FrontendClient


class DiagnosticBusController:
    """Keep diagnostic-bus cursors separate from orchestration state following."""

    def __init__(self, client: FrontendClient, *, batch: int) -> None:
        self._client = client
        self._batch = batch
        self._after_id = 0

    @property
    def after_id(self) -> int:
        return self._after_id

    async def poll(self) -> tuple[Mapping[str, object], ...]:
        response = await self._client.diagnostics.bus_tail(
            after_id=self._after_id,
            limit=self._batch,
        )
        rows: list[Mapping[str, object]] = []
        for item in response.value.items:
            if not isinstance(item, Mapping):
                continue
            row = dict(item)
            rows.append(row)
            identifier = row.get("id")
            if type(identifier) is int and identifier > self._after_id:
                self._after_id = identifier
        return tuple(rows)


__all__ = ["DiagnosticBusController"]
