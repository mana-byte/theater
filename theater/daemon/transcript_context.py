"""Retained job context shown on the newest agent-facing transcript page."""

from __future__ import annotations

from sqlalchemy import func, select

from theater.daemon.schema import jobs
from theater.models import JobKind, Participant, Tier

FIRST_PROMPT_MAX_BYTES = 2 * 1024


def newest_page_context(store, participant: Participant) -> dict[str, object]:
    """First retained prompt, later send count, and resume origin; never inferred.

    Launch jobs are GC'd by finished_at and spawns may be promptless, so this is
    the earliest retained non-empty prompt, not necessarily the original task.
    """
    if participant.tier is Tier.EXTERNAL:
        return {}
    context: dict[str, object] = {}
    first = store.conn.execute(
        select(jobs.c.prompt, jobs.c.created_at)
        .where(jobs.c.target_id == participant.id)
        .where(jobs.c.kind.in_((str(JobKind.SPAWN), str(JobKind.SEND))))
        .where(jobs.c.prompt.is_not(None))
        .where(jobs.c.prompt != "")
        .order_by(jobs.c.created_at.asc(), jobs.c.handle.asc())
        .limit(1)
    ).first()
    if first is not None:
        context["first_prompt"] = _clip(first.prompt)
        context["later_prompts"] = int(
            store.conn.execute(
                select(func.count())
                .select_from(jobs)
                .where(jobs.c.target_id == participant.id)
                .where(jobs.c.kind == str(JobKind.SEND))
                .where(jobs.c.created_at > first.created_at)
            ).scalar_one()
        )
    if participant.resumed_from_id:
        context["resumed_from"] = participant.resumed_from_id
    return context


def _clip(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= FIRST_PROMPT_MAX_BYTES:
        return text
    return encoded[:FIRST_PROMPT_MAX_BYTES].decode("utf-8", errors="ignore")
