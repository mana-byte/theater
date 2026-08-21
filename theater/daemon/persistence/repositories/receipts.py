"""Receipt-token methods and transcript-receipt persistence."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from sqlalchemy import delete, select, update

from theater.constants.daemon import RECEIPT_TOKEN_PREFIX as _RECEIPT_TOKEN_PREFIX
from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories.metadata import MetadataRepository
from theater.daemon.persistence.repositories.participants import ParticipantRepository
from theater.daemon.schema import meta, participants
from theater.models import Participant, Status
from theater.provenance import TranscriptProvenance

# Compatibility alias preserving the exact value for old importers.
RECEIPT_TOKEN_PREFIX = _RECEIPT_TOKEN_PREFIX


class ReceiptRepository:
    """Manages receipt tokens in meta and transcript receipts atomically."""

    def __init__(
        self,
        db: Database,
        meta_repo: MetadataRepository,
        participant_repo: ParticipantRepository,
    ):
        self._db = db
        self._meta = meta_repo
        self._participants = participant_repo

    def set_token(
        self,
        participant_id: str,
        token: str,
        *,
        token_path: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "token": token,
            "token_path": token_path,
        }
        self._meta.set(f"{_RECEIPT_TOKEN_PREFIX}{participant_id}", json.dumps(payload))

    def get_token(self, participant_id: str) -> str | None:
        participant = self._participants.get(participant_id)
        if participant is None or participant.status is Status.DEAD:
            self.delete_token(participant_id)
            return None
        payload = self._token_payload(participant_id)
        if payload is None:
            return None
        token = payload.get("token")
        return token if isinstance(token, str) else None

    def renew_token(self, participant_id: str) -> None:
        payload = self._token_payload(participant_id)
        if payload is None:
            return
        token = payload.get("token")
        if not isinstance(token, str):
            return
        token_path = payload.get("token_path")
        self.set_token(
            participant_id,
            token,
            token_path=token_path if isinstance(token_path, str) else None,
        )

    def delete_token(self, participant_id: str) -> None:
        payload = self._token_payload(participant_id)
        token_path = payload.get("token_path") if payload is not None else None
        if isinstance(token_path, str) and token_path:
            with contextlib.suppress(OSError):
                Path(token_path).unlink(missing_ok=True)
        self._db.conn.execute(
            delete(meta).where(meta.c.key == f"{_RECEIPT_TOKEN_PREFIX}{participant_id}")
        )

    def cleanup_tokens(self) -> int:
        rows = self._db.conn.execute(
            select(meta.c.key, meta.c.value).where(meta.c.key.like(f"{_RECEIPT_TOKEN_PREFIX}%"))
        ).fetchall()
        deleted = 0
        for key, _raw in rows:
            participant_id = key.removeprefix(_RECEIPT_TOKEN_PREFIX)
            participant = self._participants.get(participant_id)
            if participant is not None and participant.status is not Status.DEAD:
                continue
            self.delete_token(participant_id)
            deleted += 1
        return deleted

    def _token_payload(self, participant_id: str) -> dict | None:
        return self._decode_token(self._meta.get(f"{_RECEIPT_TOKEN_PREFIX}{participant_id}"))

    @staticmethod
    def _decode_token(raw: str | None) -> dict | None:
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return {"token": raw, "expires_at": 0}
        return payload if isinstance(payload, dict) else None

    def record_transcript_receipt(
        self,
        participant_id: str,
        *,
        session_id: str,
        transcript_location: str,
    ) -> Participant | None:
        """Atomically persist exact receipt provenance for a participant."""
        with self._db.engine.begin() as conn:
            conn.execute(
                update(participants)
                .where(participants.c.id == participant_id)
                .values(
                    session_id=session_id,
                    session_correlation=str(TranscriptProvenance.EXACT),
                    transcript_location=transcript_location,
                )
            )
            row = conn.execute(
                select(participants).where(participants.c.id == participant_id)
            ).first()
        return Participant.from_row(row._mapping) if row else None
