"""transcripts.read context: retained prompts and role validation."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from theater.daemon import methods as methods_mod
from theater.daemon.rpc import transcripts as transcripts_mod
from theater.models import BadRequest, Job, Tier


def _job(handle: str, target: str, kind: str, prompt: str | None, created_at: float) -> Job:
    return Job(handle, "p-parent", target, kind, prompt, "done", None, None, created_at, created_at)


async def test_newest_page_carries_retained_prompts_and_roles_are_validated(registry, monkeypatch):
    child = registry.register(harness="vibe", pane=None, cwd="/tmp")
    child = dataclasses.replace(child, tier=Tier.SPAWNED, resumed_from_id="p-old")
    registry.store.upsert_participant(child)
    registry.store.create_job(_job("spawn-1", child.id, "spawn", "", 1.0))
    registry.store.create_job(_job("send-1", child.id, "send", "é" * 2000, 2.0))
    registry.store.create_job(_job("send-2", child.id, "send", "again", 3.0))
    calls: list[dict] = []

    async def fake_page(daemon, **kwargs):
        calls.append(kwargs)
        return {"id": kwargs["participant_id"], "events": []}

    monkeypatch.setattr(transcripts_mod, "read_transcript_page", fake_page)
    daemon = SimpleNamespace(registry=registry)
    read = methods_mod.METHODS["read_transcript"]

    newest = await read(daemon, {"id": child.id, "roles": ["tool_result", "assistant"]})
    older = await read(daemon, {"id": child.id, "cursor": "trc3.x"})

    assert calls[0]["roles"] == ("assistant", "tool_result")
    assert newest["first_prompt"] == "é" * 1024
    assert newest["later_prompts"] == 1
    assert newest["resumed_from"] == "p-old"
    assert older == {"id": child.id, "events": []}
    with pytest.raises(BadRequest, match="assistant"):
        await read(daemon, {"id": child.id, "roles": ["error"]})
