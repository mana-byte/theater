"""Shared machine-wide scratchpad service and TTL policy boundary."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from sqlalchemy import Connection

from theater.daemon.persistence.repositories.scratchpad import (
    ScratchpadNamespacePage,
    ScratchpadPage,
    ScratchpadRepository,
)
from theater.daemon.persistence.transactions import WriteUnit, WriteUnitFactory
from theater.models import now

DEFAULT_SCRATCHPAD_TTL_DAYS = 7.0


class ScratchpadService:
    """One clocked service shared by private and public scratchpad routes."""

    def __init__(
        self,
        repository: ScratchpadRepository,
        write_units: WriteUnitFactory,
        *,
        ttl_days: float,
        clock: Callable[[], float] = now,
    ) -> None:
        if isinstance(ttl_days, bool):
            raise TypeError("scratchpad ttl_days must be a positive finite number")
        try:
            parsed_ttl = float(ttl_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("scratchpad ttl_days must be a positive finite number") from exc
        if not math.isfinite(parsed_ttl) or parsed_ttl <= 0:
            raise ValueError("scratchpad ttl_days must be a positive finite number")
        ttl_seconds = parsed_ttl * 24 * 60 * 60
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("scratchpad ttl_days must be a positive finite number")
        self._repository = repository
        self._write_units = write_units
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    @property
    def ttl_days(self) -> float:
        return self._ttl_seconds / (24 * 60 * 60)

    def _timestamp(self) -> float:
        timestamp = float(self._clock())
        if not math.isfinite(timestamp):
            raise ValueError("scratchpad clock must return a finite timestamp")
        return timestamp

    def write(
        self,
        *,
        namespace: str,
        value: str,
        key: str | None = None,
        actor_client_id: str | None = None,
        actor_participant_id: str | None = None,
        connection: Connection | None = None,
    ) -> str:
        """Write once with an expiry derived from the injected daemon clock."""
        timestamp = self._timestamp()
        if connection is not None:
            return self._repository.write(
                namespace=namespace,
                value=value,
                key=key,
                updated_at=timestamp,
                expires_at=timestamp + self._ttl_seconds,
                actor_client_id=actor_client_id,
                actor_participant_id=actor_participant_id,
                connection=connection,
            )
        with self._write_units() as unit:
            return self._write_at(
                unit,
                namespace=namespace,
                value=value,
                key=key,
                timestamp=timestamp,
                actor_client_id=actor_client_id,
                actor_participant_id=actor_participant_id,
            )

    def _write_at(
        self,
        unit: WriteUnit,
        *,
        namespace: str,
        value: str,
        key: str | None,
        timestamp: float,
        actor_client_id: str | None,
        actor_participant_id: str | None,
    ) -> str:
        return self._repository.write(
            namespace=namespace,
            value=value,
            key=key,
            updated_at=timestamp,
            expires_at=timestamp + self._ttl_seconds,
            actor_client_id=actor_client_id,
            actor_participant_id=actor_participant_id,
            connection=unit.connection,
        )

    def get(
        self,
        *,
        namespace: str,
        keys: list[str] | None = None,
        after_key: str | None = None,
        limit: int | None = None,
    ) -> ScratchpadPage:
        return self._repository.get(
            namespace=namespace,
            keys=keys,
            after_key=after_key,
            limit=limit,
            timestamp=self._timestamp(),
        )

    def namespaces(
        self, *, after_namespace: str | None = None, limit: int = 200
    ) -> ScratchpadNamespacePage:
        return self._repository.namespaces(
            after_namespace=after_namespace,
            limit=limit,
            timestamp=self._timestamp(),
        )

    def delete(
        self,
        *,
        namespace: str,
        keys: list[str],
        digests: list[str] | None = None,
        connection: Connection | None = None,
    ) -> list[str]:
        if connection is not None:
            return self._repository.delete(
                namespace=namespace,
                keys=keys,
                digests=digests,
                connection=connection,
            )
        with self._write_units() as unit:
            return self._repository.delete(
                namespace=namespace,
                keys=keys,
                digests=digests,
                connection=unit.connection,
            )

    def clear(self, *, namespace: str, connection: Connection | None = None) -> int:
        if connection is not None:
            return self._repository.clear(namespace=namespace, connection=connection)
        with self._write_units() as unit:
            return self._repository.clear(namespace=namespace, connection=unit.connection)

    def delete_expired(self, *, limit: int = 200) -> int:
        """Run one bounded physical-expiry sweep for coordinator-owned GC wiring."""
        with self._write_units() as unit:
            return self._repository.delete_expired(
                timestamp=self._timestamp(), limit=limit, connection=unit.connection
            )


def service_for_daemon(daemon: Any) -> ScratchpadService:
    """Use composed service when available; retain a safe transitional fallback."""
    service = getattr(daemon, "scratchpad_service", None)
    if isinstance(service, ScratchpadService):
        return service
    store = daemon.store
    repository = getattr(store, "_scratchpad", None)
    if not isinstance(repository, ScratchpadRepository):
        raise TypeError("daemon scratchpad repository is not composed")
    config = getattr(daemon, "config", None)
    section = getattr(config, "scratchpad", None)
    ttl_days = getattr(section, "ttl_days", DEFAULT_SCRATCHPAD_TTL_DAYS)
    service = ScratchpadService(repository, store.write_unit, ttl_days=ttl_days)
    daemon.scratchpad_service = service
    return service


__all__ = [
    "DEFAULT_SCRATCHPAD_TTL_DAYS",
    "ScratchpadService",
    "service_for_daemon",
]
