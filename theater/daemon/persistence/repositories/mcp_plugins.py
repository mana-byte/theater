"""Durable participant-scoped MCP-plugin grants and credential verifiers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.core import HARNESS_NAME
from theater.daemon.artifacts import ArtifactKind, remove_secret_file, validate_persisted_path
from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories.participants import ParticipantRepository
from theater.daemon.schema import participant_mcp_plugins
from theater.mcp_plugins.contracts import PluginCapability
from theater.models import Status


@dataclass(frozen=True, slots=True)
class McpPluginCredentialRecord:
    """One attached sidecar's durable, non-secret authorization facts."""

    participant_id: str
    plugin_name: str
    api_version: int
    credential_id: str
    credential_verifier: str
    grants: frozenset[PluginCapability]
    credential_path: str


class McpPluginCredentialRepository:
    """Store sidecar grants without ever retaining a plaintext credential."""

    def __init__(self, db: Database, participant_repo: ParticipantRepository) -> None:
        self._db = db
        self._participants = participant_repo

    def set(
        self,
        participant_id: str,
        *,
        plugin_name: str,
        api_version: int,
        credential_id: str,
        credential_verifier: str,
        grants: Iterable[PluginCapability],
        credential_path: str,
        connection: Connection | None = None,
    ) -> None:
        frozen_grants = _validate_grants(grants)
        if not isinstance(api_version, int) or isinstance(api_version, bool) or api_version < 1:
            raise ValueError("MCP plugin API version must be a positive integer")
        if (
            not _nonblank(plugin_name)
            or not _nonblank(credential_id)
            or not _nonblank(credential_verifier)
        ):
            raise ValueError("MCP plugin credential record contains a blank required value")
        safe_path = validate_persisted_path(
            Path(credential_path),
            owner_id=participant_id,
            kind=ArtifactKind.FILE,
        )
        values = {
            "participant_id": participant_id,
            "plugin_name": plugin_name,
            "api_version": api_version,
            "credential_id": credential_id,
            "credential_verifier": credential_verifier,
            "grants": json.dumps(sorted(capability.value for capability in frozen_grants)),
            "credential_path": str(safe_path),
        }
        conn = self._db.conn if connection is None else connection
        stmt = sqlite_insert(participant_mcp_plugins).values(**values)
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    participant_mcp_plugins.c.participant_id,
                    participant_mcp_plugins.c.plugin_name,
                ],
                set_={
                    key: value
                    for key, value in values.items()
                    if key not in {"participant_id", "plugin_name"}
                },
            )
        )

    def get_by_credential_id(self, credential_id: str) -> McpPluginCredentialRecord | None:
        if not _nonblank(credential_id):
            return None
        row = self._db.conn.execute(
            select(participant_mcp_plugins).where(
                participant_mcp_plugins.c.credential_id == credential_id
            )
        ).first()
        if row is None:
            return None
        record = self._record(row._mapping)
        if record is None:
            return None
        participant = self._participants.get(record.participant_id)
        if participant is None or participant.status is Status.DEAD:
            self.delete_participant(record.participant_id)
            return None
        return record

    def list_for_participant(self, participant_id: str) -> tuple[McpPluginCredentialRecord, ...]:
        rows = self._db.conn.execute(
            select(participant_mcp_plugins)
            .where(participant_mcp_plugins.c.participant_id == participant_id)
            .order_by(participant_mcp_plugins.c.plugin_name)
        ).fetchall()
        return tuple(record for row in rows if (record := self._record(row._mapping)) is not None)

    def delete_plugin(self, participant_id: str, plugin_name: str) -> None:
        row = self._db.conn.execute(
            select(participant_mcp_plugins.c.credential_path).where(
                participant_mcp_plugins.c.participant_id == participant_id,
                participant_mcp_plugins.c.plugin_name == plugin_name,
            )
        ).first()
        if row is not None:
            remove_secret_file(row[0], owner_id=participant_id)
        self._db.conn.execute(
            delete(participant_mcp_plugins).where(
                participant_mcp_plugins.c.participant_id == participant_id,
                participant_mcp_plugins.c.plugin_name == plugin_name,
            )
        )

    def delete_participant(self, participant_id: str) -> None:
        rows = self._db.conn.execute(
            select(participant_mcp_plugins.c.credential_path).where(
                participant_mcp_plugins.c.participant_id == participant_id
            )
        ).fetchall()
        for (credential_path,) in rows:
            remove_secret_file(credential_path, owner_id=participant_id)
        self._db.conn.execute(
            delete(participant_mcp_plugins).where(
                participant_mcp_plugins.c.participant_id == participant_id
            )
        )

    def cleanup(self) -> int:
        rows = self._db.conn.execute(
            select(participant_mcp_plugins.c.participant_id).distinct()
        ).fetchall()
        deleted = 0
        for (participant_id,) in rows:
            participant = self._participants.get(participant_id)
            if participant is not None and participant.status is not Status.DEAD:
                continue
            self.delete_participant(participant_id)
            deleted += 1
        return deleted

    @staticmethod
    def _record(row) -> McpPluginCredentialRecord | None:
        try:
            grants = _decode_grants(row["grants"])
            api_version = row["api_version"]
            if not isinstance(api_version, int) or isinstance(api_version, bool) or api_version < 1:
                return None
            values = (
                row["participant_id"],
                row["plugin_name"],
                row["credential_id"],
                row["credential_verifier"],
                row["credential_path"],
            )
            if any(not _nonblank(value) for value in values):
                return None
            if HARNESS_NAME.fullmatch(row["plugin_name"]) is None:
                return None
            if len(row["credential_verifier"]) != 64:
                return None
            int(row["credential_verifier"], 16)
            validate_persisted_path(
                Path(row["credential_path"]),
                owner_id=row["participant_id"],
                kind=ArtifactKind.FILE,
            )
        except (KeyError, TypeError, ValueError):
            return None
        return McpPluginCredentialRecord(
            participant_id=row["participant_id"],
            plugin_name=row["plugin_name"],
            api_version=api_version,
            credential_id=row["credential_id"],
            credential_verifier=row["credential_verifier"],
            grants=grants,
            credential_path=row["credential_path"],
        )


def _validate_grants(grants: Iterable[PluginCapability]) -> frozenset[PluginCapability]:
    frozen = frozenset(grants)
    if not frozen or any(not isinstance(grant, PluginCapability) for grant in frozen):
        raise ValueError("MCP plugin grants must be a non-empty set of PluginCapability values")
    return frozen


def _decode_grants(raw: object) -> frozenset[PluginCapability]:
    if not isinstance(raw, str):
        raise TypeError("stored MCP plugin grants are not text")
    decoded = json.loads(raw)
    if not isinstance(decoded, list) or not decoded:
        raise ValueError("stored MCP plugin grants are malformed")
    grants = frozenset(PluginCapability(value) for value in decoded)
    if len(grants) != len(decoded):
        raise ValueError("stored MCP plugin grants repeat a capability")
    return grants


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value)


__all__ = ["McpPluginCredentialRecord", "McpPluginCredentialRepository"]
