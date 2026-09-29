"""A completed 2.x compaction is paid model usage the observer must account exactly once."""

from __future__ import annotations

import asyncio

import pytest
from test_harness_opencode_v2 import USAGE, RecorderV2, attached, text


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def rec(tmp_path, workdir):
    r = RecorderV2(tmp_path / "opencode-v2.db", "ses_one", str(workdir))
    yield r
    r.conn.close()


def _compaction(created: int, *, status: str = "completed") -> dict:
    data: dict = {
        "time": {"created": created},
        "status": status,
        "reason": "auto",
        "summary": "the session so far",
        "recent": "",
        "model": {"id": "claude", "providerID": "anthropic"},
    }
    if status == "completed":
        data["tokens"] = {
            "input": 2000,
            "output": 300,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        }
        data["cost"] = 0.125
    return data


def test_a_completed_compaction_accounts_its_usage_once(rec, workdir):
    src = attached(rec, workdir)
    rec.user("msg_u1", "read the note")
    created = rec.tick()
    rec.step(
        "msg_a1",
        created,
        [text("pamplemousse")],
        finish="stop",
        times={"completed": rec.tick()},
        **USAGE,
    )
    rec.idle("msg_i1", "succeeded")
    rec.write("msg_c1", "compaction", _compaction(created, status="running"))
    first = asyncio.run(src.read())
    assert [e for e in first.events if e.usage_only] == []
    assert sum(e.turn_end for e in first.events) == 1

    compaction_created = rec.tick()
    rec.write("msg_c1", "compaction", _compaction(compaction_created))
    second = asyncio.run(src.read())
    (usage_event,) = [e for e in second.events if e.usage_only]
    assert usage_event.turn_end is False and usage_event.turn_terminal is None
    assert usage_event.usage.input_tokens == 2000
    assert usage_event.usage.output_tokens == 300
    assert usage_event.usage.cost_usd == 0.125
    assert usage_event.usage.idempotency_key == "opencode:msg_c1"
    assert [e for e in second.events if not e.usage_only] == []
    (fact,) = [f for f in second.trajectory if f.native_id == "msg_c1"]
    assert fact.usage is not None
    assert (fact.usage.input_tokens, fact.usage.output_tokens) == (2000, 300)
    assert fact.usage.cost_usd == 0.125
    assert second.progressed is True

    rec.write("msg_c1", "compaction", _compaction(compaction_created))
    third = asyncio.run(src.read())
    assert third.events == [] and not third.trajectory
    all_events = [*first.events, *second.events, *third.events]
    assert sum(e.turn_end for e in all_events) == 1
