"""Bounded archive reads isolated from the daemon's event loop and live sources."""

from __future__ import annotations

import asyncio

from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.harness.contracts.source import HistoryPage
from theater.harness.transcript.observer import open_participant_source
from theater.models import Participant
from theater.provenance import normalize_provenance


def read_history_page(observer, participant: Participant, pane_pid: int | None) -> HistoryPage:
    """Only detached participant facts enter this worker; no Store or Registry access."""
    source = open_participant_source(
        observer,
        participant_id=participant.id,
        cwd=participant.cwd,
        session_id=participant.session_id,
        after=None,
        session_provenance=normalize_provenance(participant.session_correlation),
        known_location=participant.transcript_location,
        transcript_domain=participant.transcript_domain,
        pane_pid=pane_pid,
    )

    async def read() -> HistoryPage:
        try:
            return await source.history_page(
                limit=TRAJECTORY_PAGE_RECORD_LIMIT, include_full_text=True
            )
        finally:
            await source.aclose()

    return asyncio.run(read())
