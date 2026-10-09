"""Independent bounded diagnostic-bus reads for the optional Régie panel."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from theater.frontend import ErrorCode, FrontendClient, FrontendResponseError

# Pause between bounded polls against a daemon that cannot long-poll.
_FALLBACK_POLL_SECONDS = 1.0


class DiagnosticBusController:
    """Keep diagnostic-bus cursors separate from orchestration state following."""

    def __init__(self, client: FrontendClient, *, batch: int) -> None:
        self._client = client
        self._batch = batch
        self._after_id = 0
        self._last_gap = 0
        self._lock = asyncio.Lock()
        self._extended = True  # daemon honours wait_seconds / kinds
        self._primed = False

    @property
    def after_id(self) -> int:
        return self._after_id

    @property
    def last_gap(self) -> int:
        return self._last_gap

    async def poll(
        self, wait_seconds: float = 0.0, *, kinds: Sequence[str] | None = None
    ) -> tuple[Mapping[str, object], ...]:
        """Read new rows; ``wait_seconds`` blocks in the daemon, ``kinds`` filters server-side."""
        async with self._lock:
            rows = await self._read(wait_seconds, kinds)
        if wait_seconds > 0 and not self._extended and not rows:
            await asyncio.sleep(_FALLBACK_POLL_SECONDS)
        return rows

    async def _read(
        self, wait_seconds: float, kinds: Sequence[str] | None
    ) -> tuple[Mapping[str, object], ...]:
        # A filtered reader starts at the bus head instead of replaying old coordination rows.
        priming = kinds is not None and not self._primed
        wanted = None if priming else kinds
        params: dict[str, object] = {
            "after_id": self._after_id,
            "limit": 1 if priming else self._batch,
        }
        extended: dict[str, object] = {}
        if wanted is not None:
            extended["kinds"] = list(wanted)
        if wait_seconds > 0 and not priming:
            extended["wait_seconds"] = wait_seconds
        if extended and self._extended:
            try:
                value = await self._tail({**params, **extended})
            except FrontendResponseError as exc:
                if exc.value.code != ErrorCode.BAD_REQUEST:
                    raise
                self._extended = False  # previous daemon: unknown parameters are refused
                value = await self._tail(params)
            else:
                if "next_after_id" not in _extra(value):
                    self._extended = False  # parameters were ignored, not honoured
        else:
            value = await self._tail(params)
        rows = _rows(value)
        scanned = max((_event_id(row) for row in rows), default=-1)
        if wanted is not None and not self._extended:
            rows = [row for row in rows if row.get("kind") in wanted]
        filtered = wanted is not None
        first_id = next(
            (identifier for row in rows if type(identifier := row.get("id")) is int), None
        )
        self._last_gap = (
            max(0, first_id - self._after_id - 1)
            if not filtered and isinstance(first_id, int) and self._after_id > 0
            else 0
        )
        cursor = _extra(value).get("next_after_id")
        for candidate in (scanned, cursor):
            if type(candidate) is int and candidate > self._after_id:
                self._after_id = candidate
        self._primed = True
        return () if priming else tuple(rows)

    async def _tail(self, params: dict[str, object]) -> object:
        response = await self._client.diagnostics.bus_tail(**params)
        return getattr(response, "value", None)


def _rows(value: object) -> list[Mapping[str, object]]:
    items = getattr(value, "items", None)
    if not isinstance(items, tuple | list):
        raise TypeError("diagnostic bus response must contain an item sequence")
    rows: list[Mapping[str, object]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise TypeError("diagnostic bus items must be mappings")
        rows.append(dict(item))
    rows.sort(key=_event_id)
    return rows


def _extra(value: object) -> Mapping[str, object]:
    extra = getattr(value, "extra", None)
    return extra if isinstance(extra, Mapping) else {}


def _event_id(row: Mapping[str, object]) -> int:
    identifier = row.get("id")
    return identifier if type(identifier) is int else -1


__all__ = ["DiagnosticBusController"]
