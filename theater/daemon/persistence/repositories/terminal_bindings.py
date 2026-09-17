"""Verified physical terminal-binding persistence."""

from __future__ import annotations

from sqlalchemy import Connection, insert, select

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
        values = row._mapping
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
