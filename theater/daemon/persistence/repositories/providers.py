"""Durable terminal-provider identities and generation allocation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Connection, insert, select, update

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
            .values(generation=providers.c.generation + 1, updated_at=updated_at)
            .returning(providers.c.generation)
        ).scalar_one_or_none()
        if generation is None:
            raise KeyError(f"unknown provider {provider_id!r}")
        return int(generation)

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
