"""Durable provider registration and bounded public projections."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from sqlalchemy.exc import IntegrityError

from theater.daemon.events.publication import catalog_invalidated_event
from theater.daemon.events.snapshot import _provider_projection
from theater.daemon.operations import OperationService
from theater.models import JournalEventRecord, ProviderRecord, TheaterError, new_id, now


class ProviderNotFound(TheaterError):
    code = "not_found"

    def __init__(self, provider_id: str) -> None:
        self.details = {"provider_id": provider_id}
        super().__init__(f"no provider {provider_id!r} exists")


class ProviderRegistryConflict(TheaterError):
    code = "ownership_conflict"

    def __init__(self, selector: object) -> None:
        self.details = {"selector": selector}
        super().__init__(f"provider selector {selector!r} is already registered")


class ProviderCursorInvalid(TheaterError):
    code = "bad_request"


class ProviderRegistry:
    def __init__(
        self,
        store,
        operations: OperationService,
        *,
        health: Callable[[str], str],
        clock: Callable[[], float] = now,
        id_factory: Callable[[], str] = new_id,
    ) -> None:
        self._store = store
        self._operations = operations
        self._health = health
        self._clock = clock
        self._id_factory = id_factory

    def register(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        def action(unit):
            timestamp = self._clock()
            record = ProviderRecord(
                provider_id=self._id_factory(),
                selector=str(params["selector"]),
                kind=str(params["kind"]),
                credential_verifier=str(params["credential_verifier"]),
                configuration_version=1,
                capabilities=tuple(str(item) for item in params["capabilities"]),
                limits=dict(params["limits"]),
                generation=0,
                last_report_revision=None,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._store.providers.register(record, connection=unit.connection)
            revision = self._store.journal.current_sequence(connection=unit.connection) + 1
            self._store.journal.append_group(
                unit,
                [
                    provider_event(record, "offline", timestamp, revision=revision),
                    catalog_invalidated_event(
                        record.provider_id,
                        revision=revision + 1,
                        recorded_at=timestamp,
                        reason="provider_registered",
                    ),
                ],
            )
            return self.project(record, health="offline")

        try:
            result = self._operations.execute_idempotent(
                client_id=client_id,
                idempotency_key=idempotency_key,
                method="frontend.providers.register",
                params=params,
                action=action,
            ).value
        except IntegrityError as exc:
            raise ProviderRegistryConflict(params["selector"]) from exc
        assert isinstance(result, Mapping)
        return result

    def update(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        health = self._health(str(params["provider_id"]))

        def action(unit):
            try:
                record = self._store.providers.update_configuration(
                    str(params["provider_id"]),
                    capabilities=(
                        tuple(str(item) for item in params["capabilities"])
                        if "capabilities" in params
                        else None
                    ),
                    limits=dict(params["limits"]) if "limits" in params else None,
                    updated_at=self._clock(),
                    connection=unit.connection,
                )
            except KeyError as exc:
                raise ProviderNotFound(str(params["provider_id"])) from exc
            revision = self._store.journal.current_sequence(connection=unit.connection) + 1
            self._store.journal.append_group(
                unit,
                [
                    provider_event(record, health, record.updated_at, revision=revision),
                    catalog_invalidated_event(
                        record.provider_id,
                        revision=revision + 1,
                        recorded_at=record.updated_at,
                        reason="provider_configuration_changed",
                    ),
                ],
            )
            return self.project(record, health=health)

        result = self._operations.execute_idempotent(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.providers.update",
            params=params,
            action=action,
        ).value
        assert isinstance(result, Mapping)
        return result

    def get(self, provider_id: str) -> ProviderRecord:
        record = self._store.providers.get(provider_id)
        if record is None:
            raise ProviderNotFound(provider_id)
        return record

    def list(
        self, *, cursor: str | None, limit: int
    ) -> tuple[tuple[ProviderRecord, ...], str | None]:
        try:
            return self._store.providers.list_page(cursor=cursor, limit=limit)
        except KeyError as exc:
            raise ProviderCursorInvalid(f"unknown provider cursor {cursor!r}") from exc

    def project(self, record: ProviderRecord, *, health: str | None = None) -> dict[str, object]:
        return _provider_projection(
            record,
            health=self._health(record.provider_id) if health is None else health,
        )


def provider_event(
    record: ProviderRecord, health: str, timestamp: float, *, revision: int
) -> JournalEventRecord:
    return JournalEventRecord(
        kind="provider.updated",
        entity_id=record.provider_id,
        entity_revision=revision,
        payload=_provider_projection(record, health=health),
        recorded_at=timestamp,
    )


__all__ = [
    "ProviderCursorInvalid",
    "ProviderNotFound",
    "ProviderRegistry",
    "ProviderRegistryConflict",
    "provider_event",
]
