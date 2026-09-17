"""Verified physical terminal-binding persistence."""

from __future__ import annotations

from sqlalchemy import Connection, insert, select, update

from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories._json import decode_json, encode_json
from theater.daemon.schema import terminal_bindings
from theater.models import TerminalBindingRecord


class TerminalBindingRepository:
    def __init__(self, db: Database):
        self._db = db

    def bind(self, binding: TerminalBindingRecord, *, connection: Connection) -> None:
        connection.execute(
            insert(terminal_bindings).values(
                participant_id=binding.participant_id,
                provider_id=binding.provider_id,
                provider_generation=binding.provider_generation,
                terminal_id=binding.terminal_id,
                terminal_incarnation=binding.terminal_incarnation,
                process_facts=(
                    None
                    if binding.process_facts is None
                    else encode_json(dict(binding.process_facts))
                ),
                occupant_evidence=encode_json(dict(binding.occupant_evidence)),
                health=binding.health,
                report_revision=binding.report_revision,
                created_at=binding.created_at,
                updated_at=binding.updated_at,
            )
        )

    def get(
        self, participant_id: str, *, connection: Connection | None = None
    ) -> TerminalBindingRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(terminal_bindings).where(terminal_bindings.c.participant_id == participant_id)
        ).first()
        if row is None:
            return None
        return self._from_row(row._mapping)

    def list_for_provider(
        self,
        provider_id: str,
        *,
        connection: Connection | None = None,
    ) -> tuple[TerminalBindingRecord, ...]:
        conn = self._db.conn if connection is None else connection
        rows = conn.execute(
            select(terminal_bindings)
            .where(terminal_bindings.c.provider_id == provider_id)
            .order_by(terminal_bindings.c.terminal_id, terminal_bindings.c.participant_id)
        ).all()
        return tuple(self._from_row(row._mapping) for row in rows)

    def restore_generation(
        self,
        participant_id: str,
        *,
        previous_generation: int,
        provider_generation: int,
        report_revision: int,
        health: str,
        updated_at: float,
        connection: Connection,
    ) -> bool:
        updated = connection.execute(
            update(terminal_bindings)
            .where(
                terminal_bindings.c.participant_id == participant_id,
                terminal_bindings.c.provider_generation == previous_generation,
            )
            .values(
                provider_generation=provider_generation,
                report_revision=report_revision,
                health=health,
                updated_at=updated_at,
            )
        )
        return bool(updated.rowcount)

    def update_health(
        self,
        participant_id: str,
        *,
        provider_generation: int,
        report_revision: int,
        health: str,
        updated_at: float,
        connection: Connection,
    ) -> bool:
        updated = connection.execute(
            update(terminal_bindings)
            .where(
                terminal_bindings.c.participant_id == participant_id,
                terminal_bindings.c.provider_generation == provider_generation,
                terminal_bindings.c.report_revision < report_revision,
            )
            .values(
                report_revision=report_revision,
                health=health,
                updated_at=updated_at,
            )
        )
        return bool(updated.rowcount)

    @staticmethod
    def _from_row(values) -> TerminalBindingRecord:
        occupant = decode_json(str(values["occupant_evidence"]))
        process = (
            None if values["process_facts"] is None else decode_json(str(values["process_facts"]))
        )
        if not isinstance(occupant, dict) or (
            process is not None and not isinstance(process, dict)
        ):
            raise ValueError("stored terminal evidence is invalid")
        return TerminalBindingRecord(
            participant_id=str(values["participant_id"]),
            provider_id=str(values["provider_id"]),
            provider_generation=int(values["provider_generation"]),
            terminal_id=str(values["terminal_id"]),
            terminal_incarnation=str(values["terminal_incarnation"]),
            process_facts=process,
            occupant_evidence=occupant,
            health=str(values["health"]),
            report_revision=int(values["report_revision"]),
            created_at=float(values["created_at"]),
            updated_at=float(values["updated_at"]),
        )


__all__ = ["TerminalBindingRepository"]
