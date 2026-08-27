"""Participant-scoped credentials for bounded native signal channels."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from theater.constants.daemon import CHANNEL_CREDENTIAL_PREFIX
from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories.metadata import MetadataRepository
from theater.daemon.persistence.repositories.participants import ParticipantRepository
from theater.daemon.schema import meta
from theater.harness.contracts.channels import ChannelKind
from theater.models import Status


@dataclass(frozen=True, slots=True)
class ChannelCredentialRecord:
    """One persisted participant-scoped native channel credential."""

    harness: str
    kind: ChannelKind
    channel_id: str
    token: str
    token_path: str


class ChannelCredentialRepository:
    """Persist only credentials required by native channels after restart."""

    def __init__(
        self,
        db: Database,
        meta_repo: MetadataRepository,
        participant_repo: ParticipantRepository,
    ) -> None:
        self._db = db
        self._meta = meta_repo
        self._participants = participant_repo

    def set(
        self,
        participant_id: str,
        *,
        harness: str,
        kind: ChannelKind,
        channel_id: str,
        token: str,
        token_path: str,
    ) -> None:
        self._meta.set(
            self._key(participant_id, kind, channel_id),
            json.dumps(
                {
                    "harness": harness,
                    "kind": kind.value,
                    "channel_id": channel_id,
                    "token": token,
                    "token_path": token_path,
                }
            ),
        )

    def get(
        self,
        participant_id: str,
        kind: ChannelKind,
        channel_id: str,
    ) -> ChannelCredentialRecord | None:
        participant = self._participants.get(participant_id)
        if participant is None or participant.status is Status.DEAD:
            self.delete_participant(participant_id)
            return None
        raw = self._meta.get(self._key(participant_id, kind, channel_id))
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        fields = ("harness", "kind", "channel_id", "token", "token_path")
        if any(not isinstance(payload.get(field), str) or not payload[field] for field in fields):
            return None
        try:
            stored_kind = ChannelKind(payload["kind"])
        except ValueError:
            return None
        if stored_kind is not kind or payload["channel_id"] != channel_id:
            return None
        return ChannelCredentialRecord(
            harness=payload["harness"],
            kind=stored_kind,
            channel_id=payload["channel_id"],
            token=payload["token"],
            token_path=payload["token_path"],
        )

    def delete_participant(self, participant_id: str) -> None:
        prefix = f"{CHANNEL_CREDENTIAL_PREFIX}{participant_id}:"
        rows = self._db.conn.execute(
            select(meta.c.key, meta.c.value).where(meta.c.key.like(f"{CHANNEL_CREDENTIAL_PREFIX}%"))
        ).fetchall()
        for key, raw in rows:
            if not key.startswith(prefix):
                continue
            self._unlink_token(raw)
            self._db.conn.execute(delete(meta).where(meta.c.key == key))

    def cleanup(self) -> int:
        rows = self._db.conn.execute(
            select(meta.c.key).where(meta.c.key.like(f"{CHANNEL_CREDENTIAL_PREFIX}%"))
        ).fetchall()
        participant_ids = {
            key.removeprefix(CHANNEL_CREDENTIAL_PREFIX).split(":", 1)[0] for (key,) in rows
        }
        deleted = 0
        for participant_id in participant_ids:
            participant = self._participants.get(participant_id)
            if participant is not None and participant.status is not Status.DEAD:
                continue
            self.delete_participant(participant_id)
            deleted += 1
        return deleted

    @staticmethod
    def _key(participant_id: str, kind: ChannelKind, channel_id: str) -> str:
        return f"{CHANNEL_CREDENTIAL_PREFIX}{participant_id}:{kind.value}:{channel_id}"

    @staticmethod
    def _unlink_token(raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        token_path = payload.get("token_path") if isinstance(payload, dict) else None
        if isinstance(token_path, str) and token_path:
            with contextlib.suppress(OSError):
                Path(token_path).unlink(missing_ok=True)


__all__ = ["ChannelCredentialRecord", "ChannelCredentialRepository"]
