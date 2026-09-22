"""Public operations, launch reservations, and idempotency claims."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Connection, delete, exists, func, insert, or_, select, update

from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories._json import decode_json, encode_json
from theater.daemon.schema import idempotency_records, launch_reservations, public_operations
from theater.models import IdempotencyRecord, LaunchReservationRecord, PublicOperationRecord


class OperationRepository:
    def __init__(self, db: Database):
        self._db = db

    def create(self, record: PublicOperationRecord, *, connection: Connection) -> None:
        connection.execute(insert(public_operations).values(**self._operation_values(record)))

    def get(
        self, operation_id: str, *, connection: Connection | None = None
    ) -> PublicOperationRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(public_operations).where(public_operations.c.operation_id == operation_id)
        ).first()
        return self._operation_from_row(dict(row._mapping)) if row else None

    def list_page(
        self,
        *,
        cursor: str | None,
        limit: int,
        unsettled_only: bool = False,
        target_id: str | None = None,
        connection: Connection | None = None,
    ) -> tuple[tuple[PublicOperationRecord, ...], str | None]:
        """Return a stable newest-first page using an operation ID cursor."""
        conn = self._db.conn if connection is None else connection
        query = select(public_operations)
        if cursor is not None:
            cursor_row = conn.execute(
                select(public_operations.c.created_at, public_operations.c.operation_id).where(
                    public_operations.c.operation_id == cursor
                )
            ).first()
            if cursor_row is None:
                raise KeyError(cursor)
            created_at, operation_id = cursor_row
            query = query.where(
                or_(
                    public_operations.c.created_at < created_at,
                    (
                        (public_operations.c.created_at == created_at)
                        & (public_operations.c.operation_id < operation_id)
                    ),
                )
            )
        if unsettled_only:
            query = query.where(public_operations.c.state.in_(("accepted", "running", "uncertain")))
        if target_id is not None:
            targets = func.json_each(public_operations.c.target_ids).table_valued("value")
            query = query.where(
                exists(select(1).select_from(targets).where(targets.c.value == target_id))
            )
        rows = conn.execute(
            query.order_by(
                public_operations.c.created_at.desc(), public_operations.c.operation_id.desc()
            ).limit(limit + 1)
        ).all()
        records = tuple(self._operation_from_row(dict(row._mapping)) for row in rows[:limit])
        next_cursor = records[-1].operation_id if len(rows) > limit else None
        return records, next_cursor

    def replace(
        self,
        record: PublicOperationRecord,
        *,
        expected_state: str,
        expected_updated_at: float,
        connection: Connection,
    ) -> bool:
        values = self._operation_values(record)
        values.pop("operation_id")
        updated = connection.execute(
            update(public_operations)
            .where(
                public_operations.c.operation_id == record.operation_id,
                public_operations.c.state == expected_state,
                public_operations.c.updated_at == expected_updated_at,
            )
            .values(**values)
        )
        return bool(updated.rowcount)

    def update_state(
        self,
        operation_id: str,
        *,
        state: str,
        phase: str,
        updated_at: float,
        result: object | None = None,
        error_code: str | None = None,
        error: Mapping[str, object] | None = None,
        settled_at: float | None = None,
        connection: Connection,
    ) -> bool:
        updated = connection.execute(
            update(public_operations)
            .where(public_operations.c.operation_id == operation_id)
            .values(
                state=state,
                phase=phase,
                result=None if result is None else encode_json(result),
                error_code=error_code,
                error=None if error is None else encode_json(dict(error)),
                updated_at=updated_at,
                settled_at=settled_at,
            )
        )
        return bool(updated.rowcount)

    def reserve_launch(
        self, reservation: LaunchReservationRecord, *, connection: Connection
    ) -> None:
        connection.execute(
            insert(launch_reservations).values(
                operation_id=reservation.operation_id,
                participant_id=reservation.participant_id,
                provider_id=reservation.provider_id,
                workspace_usage_id=reservation.workspace_usage_id,
                adapter=reservation.adapter,
                phase=reservation.phase,
                launch_facts=encode_json(dict(reservation.launch_facts)),
                artifact_refs=encode_json(list(reservation.artifact_refs)),
                dispatch_marker=reservation.dispatch_marker,
                created_at=reservation.created_at,
                updated_at=reservation.updated_at,
            )
        )

    def get_launch(
        self, operation_id: str, *, connection: Connection | None = None
    ) -> LaunchReservationRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(launch_reservations).where(launch_reservations.c.operation_id == operation_id)
        ).first()
        return self._launch_from_row(dict(row._mapping)) if row is not None else None

    def launch_for_participant(
        self, participant_id: str, *, connection: Connection | None = None
    ) -> LaunchReservationRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(launch_reservations).where(
                launch_reservations.c.participant_id == participant_id
            )
        ).first()
        return self._launch_from_row(dict(row._mapping)) if row is not None else None

    def claim_idempotency(self, record: IdempotencyRecord, *, connection: Connection) -> None:
        connection.execute(
            insert(idempotency_records).values(
                client_id=record.client_id,
                key=record.key,
                method=record.method,
                payload_digest=record.payload_digest,
                operation_id=record.operation_id,
                response=(None if record.response is None else encode_json(record.response)),
                created_at=record.created_at,
                settled_at=record.settled_at,
                retain_until=record.retain_until,
            )
        )

    def get_idempotency(
        self,
        client_id: str,
        key: str,
        *,
        connection: Connection | None = None,
    ) -> IdempotencyRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(idempotency_records).where(
                idempotency_records.c.client_id == client_id,
                idempotency_records.c.key == key,
            )
        ).first()
        if row is None:
            return None
        values = row._mapping
        return IdempotencyRecord(
            client_id=str(values["client_id"]),
            key=str(values["key"]),
            method=str(values["method"]),
            payload_digest=str(values["payload_digest"]),
            operation_id=(None if values["operation_id"] is None else str(values["operation_id"])),
            response=(None if values["response"] is None else decode_json(str(values["response"]))),
            created_at=float(values["created_at"]),
            settled_at=(None if values["settled_at"] is None else float(values["settled_at"])),
            retain_until=(
                None if values["retain_until"] is None else float(values["retain_until"])
            ),
        )

    def complete_idempotency(
        self,
        client_id: str,
        key: str,
        *,
        response: object,
        settled_at: float | None,
        retain_until: float | None,
        connection: Connection,
    ) -> bool:
        updated = connection.execute(
            update(idempotency_records)
            .where(
                idempotency_records.c.client_id == client_id,
                idempotency_records.c.key == key,
            )
            .values(
                response=encode_json(response),
                settled_at=settled_at,
                retain_until=retain_until,
            )
        )
        return bool(updated.rowcount)

    def settle_idempotency_for_operation(
        self,
        operation_id: str,
        *,
        settled_at: float,
        retain_until: float,
        connection: Connection,
    ) -> int:
        updated = connection.execute(
            update(idempotency_records)
            .where(idempotency_records.c.operation_id == operation_id)
            .values(settled_at=settled_at, retain_until=retain_until)
        )
        return int(updated.rowcount or 0)

    def delete_idempotency(self, client_id: str, key: str, *, connection: Connection) -> bool:
        deleted = connection.execute(
            delete(idempotency_records).where(
                idempotency_records.c.client_id == client_id,
                idempotency_records.c.key == key,
            )
        )
        return bool(deleted.rowcount)

    @staticmethod
    def _operation_values(record: PublicOperationRecord) -> dict[str, object]:
        return {
            "operation_id": record.operation_id,
            "kind": record.kind,
            "actor_client_id": record.actor_client_id,
            "actor_participant_id": record.actor_participant_id,
            "target_ids": encode_json(list(record.target_ids)),
            "state": record.state,
            "phase": record.phase,
            "control_operation_id": record.control_operation_id,
            "job_handle": record.job_handle,
            "result": None if record.result is None else encode_json(record.result),
            "error_code": record.error_code,
            "error": None if record.error is None else encode_json(dict(record.error)),
            "dispatch_provider_id": record.dispatch_provider_id,
            "dispatch_provider_generation": record.dispatch_provider_generation,
            "dispatch_terminal_id": record.dispatch_terminal_id,
            "dispatch_terminal_incarnation": record.dispatch_terminal_incarnation,
            "dispatch_terminal_occupant_evidence": (
                None
                if record.dispatch_terminal_occupant_evidence is None
                else encode_json(dict(record.dispatch_terminal_occupant_evidence))
            ),
            "dispatch_terminal_process_facts": (
                None
                if record.dispatch_terminal_process_facts is None
                else encode_json(dict(record.dispatch_terminal_process_facts))
            ),
            "dispatch_backend_generation": record.dispatch_backend_generation,
            "dispatch_native_session_id": record.dispatch_native_session_id,
            "dispatch_native_turn_id": record.dispatch_native_turn_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "settled_at": record.settled_at,
        }

    @staticmethod
    def _operation_from_row(row: Mapping[str, Any]) -> PublicOperationRecord:
        raw_targets = decode_json(str(row["target_ids"]))
        if not isinstance(raw_targets, list) or not all(
            isinstance(item, str) for item in raw_targets
        ):
            raise ValueError("stored operation target IDs are invalid")
        raw_error = None if row["error"] is None else decode_json(str(row["error"]))
        if raw_error is not None and not isinstance(raw_error, dict):
            raise ValueError("stored operation error is invalid")
        return PublicOperationRecord(
            operation_id=str(row["operation_id"]),
            kind=str(row["kind"]),
            actor_client_id=str(row["actor_client_id"]),
            actor_participant_id=(
                None if row["actor_participant_id"] is None else str(row["actor_participant_id"])
            ),
            target_ids=tuple(raw_targets),
            state=str(row["state"]),
            phase=str(row["phase"]),
            control_operation_id=(
                None if row["control_operation_id"] is None else str(row["control_operation_id"])
            ),
            job_handle=None if row["job_handle"] is None else str(row["job_handle"]),
            result=None if row["result"] is None else decode_json(str(row["result"])),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            error=raw_error,
            dispatch_provider_id=(
                None if row["dispatch_provider_id"] is None else str(row["dispatch_provider_id"])
            ),
            dispatch_provider_generation=_optional_int(row["dispatch_provider_generation"]),
            dispatch_terminal_id=(
                None if row["dispatch_terminal_id"] is None else str(row["dispatch_terminal_id"])
            ),
            dispatch_terminal_incarnation=(
                None
                if row["dispatch_terminal_incarnation"] is None
                else str(row["dispatch_terminal_incarnation"])
            ),
            dispatch_terminal_occupant_evidence=_optional_json_object(
                row["dispatch_terminal_occupant_evidence"],
                "operation terminal occupant evidence",
            ),
            dispatch_terminal_process_facts=_optional_json_object(
                row["dispatch_terminal_process_facts"],
                "operation terminal process facts",
            ),
            dispatch_backend_generation=_optional_int(row["dispatch_backend_generation"]),
            dispatch_native_session_id=(
                None
                if row["dispatch_native_session_id"] is None
                else str(row["dispatch_native_session_id"])
            ),
            dispatch_native_turn_id=(
                None
                if row["dispatch_native_turn_id"] is None
                else str(row["dispatch_native_turn_id"])
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            settled_at=None if row["settled_at"] is None else float(row["settled_at"]),
        )

    @staticmethod
    def _launch_from_row(row: Mapping[str, Any]) -> LaunchReservationRecord:
        facts = decode_json(str(row["launch_facts"]))
        artifacts = decode_json(str(row["artifact_refs"]))
        if (
            not isinstance(facts, dict)
            or not isinstance(artifacts, list)
            or not all(isinstance(item, str) for item in artifacts)
        ):
            raise ValueError("stored launch reservation payload is invalid")
        return LaunchReservationRecord(
            operation_id=str(row["operation_id"]),
            participant_id=str(row["participant_id"]),
            provider_id=str(row["provider_id"]),
            workspace_usage_id=(
                None if row["workspace_usage_id"] is None else str(row["workspace_usage_id"])
            ),
            adapter=str(row["adapter"]),
            phase=str(row["phase"]),
            launch_facts=facts,
            artifact_refs=tuple(artifacts),
            dispatch_marker=(
                None if row["dispatch_marker"] is None else str(row["dispatch_marker"])
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_json_object(value: Any, label: str) -> Mapping[str, object] | None:
    if value is None:
        return None
    decoded = decode_json(str(value))
    if not isinstance(decoded, dict):
        raise TypeError(f"stored {label} is invalid")
    return decoded


__all__ = ["OperationRepository"]
