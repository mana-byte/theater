"""Durable terminal-provider identities and generation allocation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Connection, insert, or_, select, update

from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories._json import decode_json, encode_json
from theater.daemon.schema import providers
from theater.models import ProviderRecord


class ProviderRepository:
    def __init__(self, db: Database):
        self._db = db

    def register(self, record: ProviderRecord, *, connection: Connection) -> None:
        connection.execute(
            insert(providers).values(
                provider_id=record.provider_id,
                selector=record.selector,
                kind=record.kind,
                credential_verifier=record.credential_verifier,
                configuration_version=record.configuration_version,
                capabilities=encode_json(list(record.capabilities)),
                limits=encode_json(dict(record.limits)),
                generation=record.generation,
                last_report_revision=record.last_report_revision,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )

    def get(
        self, provider_id: str, *, connection: Connection | None = None
    ) -> ProviderRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(select(providers).where(providers.c.provider_id == provider_id)).first()
        return self._from_row(dict(row._mapping)) if row else None

    def get_by_selector(
        self, selector: str, *, connection: Connection | None = None
    ) -> ProviderRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(select(providers).where(providers.c.selector == selector)).first()
        return self._from_row(dict(row._mapping)) if row else None

    def list_page(
        self,
        *,
        cursor: str | None,
        limit: int,
        connection: Connection | None = None,
    ) -> tuple[tuple[ProviderRecord, ...], str | None]:
        conn = self._db.conn if connection is None else connection
        query = select(providers)
        if cursor is not None:
            cursor_row = conn.execute(
                select(providers.c.selector, providers.c.provider_id).where(
                    providers.c.provider_id == cursor
                )
            ).first()
            if cursor_row is None:
                raise KeyError(cursor)
            selector, provider_id = cursor_row
            query = query.where(
                or_(
                    providers.c.selector > selector,
                    (providers.c.selector == selector) & (providers.c.provider_id > provider_id),
                )
            )
        rows = conn.execute(
            query.order_by(providers.c.selector, providers.c.provider_id).limit(limit + 1)
        ).all()
        records = tuple(self._from_row(dict(row._mapping)) for row in rows[:limit])
        next_cursor = records[-1].provider_id if len(rows) > limit else None
        return records, next_cursor

    def update_configuration(
        self,
        provider_id: str,
        *,
        capabilities: tuple[str, ...] | None,
        limits: Mapping[str, object] | None,
        updated_at: float,
        connection: Connection,
    ) -> ProviderRecord:
        values: dict[str, object] = {
            "configuration_version": providers.c.configuration_version + 1,
            "updated_at": updated_at,
        }
        if capabilities is not None:
            values["capabilities"] = encode_json(list(capabilities))
        if limits is not None:
            values["limits"] = encode_json(dict(limits))
        row = connection.execute(
            update(providers)
            .where(providers.c.provider_id == provider_id)
            .values(**values)
            .returning(*providers.c)
        ).first()
        if row is None:
            raise KeyError(provider_id)
        return self._from_row(dict(row._mapping))

    def claim_generation(
        self,
        provider_id: str,
        *,
        updated_at: float,
        connection: Connection,
    ) -> int:
        generation = connection.execute(
            update(providers)
            .where(providers.c.provider_id == provider_id)
            .values(
                generation=providers.c.generation + 1,
                last_report_revision=None,
                updated_at=updated_at,
            )
            .returning(providers.c.generation)
        ).scalar_one_or_none()
        if generation is None:
            raise KeyError(f"unknown provider {provider_id!r}")
        return int(generation)

    def accept_report_revision(
        self,
        provider_id: str,
        *,
        generation: int,
        report_revision: int,
        updated_at: float,
        connection: Connection,
    ) -> bool:
        updated = connection.execute(
            update(providers)
            .where(
                providers.c.provider_id == provider_id,
                providers.c.generation == generation,
                or_(
                    providers.c.last_report_revision.is_(None),
                    providers.c.last_report_revision < report_revision,
                ),
            )
            .values(last_report_revision=report_revision, updated_at=updated_at)
        )
        return bool(updated.rowcount)

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> ProviderRecord:
        capabilities = decode_json(str(row["capabilities"]))
        limits = decode_json(str(row["limits"]))
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) for item in capabilities
        ):
            raise TypeError("stored provider capabilities are invalid")
        if not isinstance(limits, dict):
            raise TypeError("stored provider limits are invalid")
        return ProviderRecord(
            provider_id=str(row["provider_id"]),
            selector=str(row["selector"]),
            kind=str(row["kind"]),
            credential_verifier=str(row["credential_verifier"]),
            configuration_version=int(row["configuration_version"]),
            capabilities=tuple(capabilities),
            limits=limits,
            generation=int(row["generation"]),
            last_report_revision=(
                None if row["last_report_revision"] is None else int(row["last_report_revision"])
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


__all__ = ["ProviderRepository"]
