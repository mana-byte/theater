"""A native send is a prompt: its `theater:send` entry is the only identity its turn has."""

from __future__ import annotations

import asyncio

from tests.support.pi_transcripts import append, message_record, session_record
from theater.harness.builtin.plugins.pi.observer import PiObserver


def _send(entry_id: str, prompt: str) -> dict[str, object]:
    return {
        "type": "custom_message",
        "id": entry_id,
        "timestamp": "2026-08-31T12:00:01.000Z",
        "customType": "theater:send",
        "content": [{"type": "text", "text": prompt}],
        "display": True,
        "details": {"operation_id": "op-1"},
    }


def test_a_native_send_is_heard_and_names_the_turn_that_answers_it(tmp_path) -> None:
    workdir, root = tmp_path / "work", tmp_path / "sessions"
    workdir.mkdir()
    root.mkdir()
    transcript = root / "session.jsonl"
    reply = {"role": "assistant", "content": "done", "stopReason": "stop"}
    append(
        transcript,
        session_record(session_id="s", cwd=workdir),
        _send("send-1", "Reply with ROUTE-1"),
        message_record("asst-1", reply),
    )
    source = PiObserver(root=root, isolated=True).open_source(cwd=str(workdir), session_id="s")
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    # attach at the cold boundary replays the file: the prompt arrives before the turn end
    batch = asyncio.run(source.read())
    heard = [(e.kind, e.text, e.turn_id) for e in batch.events if e.text == "Reply with ROUTE-1"]
    assert heard == [("user", "Reply with ROUTE-1", "send-1")]
    (end,) = [e for e in batch.events if e.turn_end]
    assert end.turn_id == "send-1"
