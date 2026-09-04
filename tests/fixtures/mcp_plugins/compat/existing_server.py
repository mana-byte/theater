from __future__ import annotations

from collections.abc import Callable


class ExistingParticipantServer:
    def participant(self, participant_id: str, lookup: Callable[[str], dict]) -> dict:
        row = lookup(participant_id)
        return {"id": row["id"], "source": "existing-server"}
