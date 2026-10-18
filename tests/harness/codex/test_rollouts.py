"""Codex paginated-rollout support: native `item_completed` records.

Modern Codex defaults non-ephemeral threads to paginated history. Those
rollouts persist ``item_completed`` TurnItems and drop the legacy
``user_message`` / ``agent_message`` / ``mcp_tool_call_*`` / ``patch_apply_end``
events, so without handling the items the observer never hears the prompt and
an unrelated human turn can resolve a pending job.

The wire shapes below are taken from the native Rust types
(``codex-rs/protocol/src/items.rs``, ``protocol.rs``): TurnItem tags are
PascalCase (``#[serde(tag = "type")]`` without rename), ``UserMessage``
content blocks are ``{"type": "text", "text": ...}`` snake_case user input,
``AgentMessage`` content blocks are ``{"type": "Text", "text": ...}``, and the
``ItemCompletedEvent`` payload carries ``thread_id`` / ``turn_id`` /
``started_at_ms`` / ``completed_at_ms``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theater.daemon.observation.turns import TurnAccumulator, answers_prompt
from theater.daemon.trajectory.project import project_events_and_facts
from theater.harness.base import EventKind
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.provenance import TranscriptProvenance
from theater.trajectory.enums import TrajectoryKind, TrajectoryStatus

_THREAD = "01a0b0c0-0000-7000-8000-000000000001"
_TS = "2026-09-10T14:00:00.000Z"
_CWD = "/repo"


def _record(kind: str, payload: dict, ts: str = _TS) -> dict:
    return {"timestamp": ts, "type": kind, "payload": payload}


def _item_completed(turn_id: str, item: dict, *, started_ms: int, completed_ms: int) -> dict:
    return _record(
        "event_msg",
        {
            "type": "item_completed",
            "thread_id": _THREAD,
            "turn_id": turn_id,
            "item": item,
            "started_at_ms": started_ms,
            "completed_at_ms": completed_ms,
        },
    )


def _session_meta() -> dict:
    return _record("session_meta", {"id": _THREAD, "cwd": _CWD, "model": "gpt-5.6-sol"})


def _raw_message(turn_id: str, role: str, message_id: str, text: str, block_type: str) -> dict:
    return _record(
        "response_item",
        {
            "type": "message",
            "id": message_id,
            "role": role,
            "content": [{"type": block_type, "text": text}],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        },
    )


def _paginated_turn(
    turn_id: str,
    prompt: str,
    reply: str,
    *,
    extra: tuple[dict, ...] = (),
) -> list[dict]:
    """One modern paginated turn, both representations persisted.

    Mirrors a real paginated rollout: the raw user/assistant ``response_item``
    records appear alongside the ``item_completed`` UserMessage/AgentMessage
    items, and only ``task_complete`` closes the turn.
    """
    return [
        _record("event_msg", {"type": "task_started", "turn_id": turn_id}),
        _raw_message(turn_id, "user", f"raw-user-{turn_id}", prompt, "input_text"),
        _item_completed(
            turn_id,
            {
                "type": "UserMessage",
                "id": f"user-item-{turn_id}",
                "content": [{"type": "text", "text": prompt, "text_elements": []}],
            },
            started_ms=1789040000000,
            completed_ms=1789040000500,
        ),
        _item_completed(
            turn_id,
            {
                "type": "AgentMessage",
                "id": f"agent-item-{turn_id}",
                "content": [{"type": "Text", "text": reply}],
                "phase": "final_answer",
            },
            started_ms=1789040001000,
            completed_ms=1789040002000,
        ),
        _raw_message(turn_id, "assistant", f"agent-item-{turn_id}", reply, "output_text"),
        *extra,
        _record(
            "event_msg",
            {"type": "task_complete", "turn_id": turn_id, "last_agent_message": reply},
        ),
    ]


def _parsed(observer: CodexObserver, records: list[dict]):
    return [
        observer.parse_record(json.dumps(record), index) for index, record in enumerate(records)
    ]


def _events(parsed) -> list:
    return [event for record in parsed for event in record.events]


def _facts(parsed) -> list:
    return [fact for record in parsed for fact in record.trajectory]


def _accumulate(events: list):
    """Feed control events the way the observation reducer does."""
    turns = []
    accumulator = TurnAccumulator()
    for event in events:
        if event.usage_only:
            continue
        if event.kind is EventKind.USER:
            accumulator.hear(event.text)
        elif event.kind is EventKind.ASSISTANT:
            accumulator.say(event.text, event.raw_text)
        if event.turn_end:
            turns.append(accumulator.take())
    return turns


def _mcp_item(item_id: str, *, server: str = "theater", tool: str = "whoami") -> dict:
    return {
        "type": "McpToolCall",
        "id": item_id,
        "server": server,
        "tool": tool,
        "arguments": {"name": "audit"},
        "status": "completed",
        "result": {
            "content": [{"type": "text", "text": "whoami: ok"}],
            "isError": False,
        },
        "duration": {"secs": 1, "nanos": 500_000_000},
    }


def _file_change_item(item_id: str, *, status: str = "completed") -> dict:
    return {
        "type": "FileChange",
        "id": item_id,
        "changes": {"theater/new.py": {"type": "add", "content": "x = 1\n"}},
        "status": status,
        "auto_approved": True,
        "stdout": "Applied patch.",
        "stderr": "",
    }


# ---- native item identity for live/durable reconciliation ------------------


def test_item_completed_events_carry_exact_native_identity():
    """The durable parser stamps the same native ids the live source emits.

    Live normalization and the durable transcript describe one native item;
    both carry its exact id (and revision, when the rollout has one) so the
    composition reconciles them by identity instead of emitting duplicates.
    """
    observer = CodexObserver()
    records = [
        _session_meta(),
        _item_completed(
            "turn-a",
            {
                "type": "UserMessage",
                "id": "user-item-a",
                "content": [{"type": "text", "text": "do it"}],
            },
            started_ms=1789040000000,
            completed_ms=1789040000500,
        ),
        _item_completed(
            "turn-a",
            {
                "type": "AgentMessage",
                "id": "agent-item-a",
                "revision": 3,
                "content": [{"type": "Text", "text": "done"}],
            },
            started_ms=1789040001000,
            completed_ms=1789040002000,
        ),
    ]
    events = _events(_parsed(observer, records))

    user_events = [event for event in events if event.kind is EventKind.USER]
    assistant_events = [event for event in events if event.kind is EventKind.ASSISTANT]
    assert [event.native_id for event in user_events] == ["user-item-a"]
    assert user_events[0].revision == 0
    assert [(event.native_id, event.revision) for event in assistant_events] == [
        ("agent-item-a", 3)
    ]


def test_multi_event_tool_items_stay_anonymous_to_avoid_collapsing():
    """MCP/file items emit several control events; a shared id would collapse them.

    They have no live event counterpart, so they keep legacy anonymity.
    """
    observer = CodexObserver()
    records = [
        _session_meta(),
        _item_completed(
            "turn-a",
            {
                "type": "FileChange",
                "id": "change-item-a",
                "changes": {"/repo/x.py": {"type": "add", "content": "x = 1"}},
            },
            started_ms=1789040000000,
            completed_ms=1789040000500,
        ),
    ]
    events = _events(_parsed(observer, records))

    tool_events = [event for event in events if event.kind is EventKind.TOOL_CALL]
    assert len(tool_events) == 1
    assert tool_events[0].native_id is None


# ---- the prompt gate ------------------------------------------------------


def test_paginated_prompt_is_heard_from_the_user_item():
    """Only the item carries the prompt; the raw user record stays control-only.

    The raw ``response_item`` role=user records include environment context
    (AGENTS.md, `<environment_context>`) that must never be heard, so the
    USER event comes from the ``item_completed`` UserMessage alone.
    """
    observer = CodexObserver()
    records = [
        _session_meta(),
        *_paginated_turn(
            "turn-a",
            "audit the repo for correctness",
            "done",
            extra=(
                _raw_message(
                    "turn-a", "user", "raw-env-context", "# AGENTS.md instructions", "input_text"
                ),
            ),
        ),
    ]
    events = _events(_parsed(observer, records))

    user_events = [event for event in events if event.kind is EventKind.USER]
    assert [event.text for event in user_events] == ["audit the repo for correctness"]
    assert user_events[0].raw_text == "audit the repo for correctness"
    assert user_events[0].turn_id == "turn-a"

    # Environment-context user records produce no control events at all.
    assert not any("# AGENTS.md instructions" in (event.text or "") for event in events)


def test_an_unrelated_human_turn_does_not_answer_the_pending_prompt():
    """Two paginated turns: each boundary answers only its own prompt."""
    observer = CodexObserver()
    records = [
        _session_meta(),
        *_paginated_turn("turn-a", "do the codex audit", "audit done"),
        *_paginated_turn("turn-b", "hello again", "hi there"),
    ]
    events = _events(_parsed(observer, records))
    turns = _accumulate(events)

    boundaries = [event for event in events if event.turn_end]
    assert [event.turn_id for event in boundaries] == ["turn-a", "turn-b"]
    assert len(turns) == 2

    assert answers_prompt(turns[0].heard, "do the codex audit") is True
    assert answers_prompt(turns[0].heard, "hello again") is False
    assert answers_prompt(turns[1].heard, "do the codex audit") is False
    assert answers_prompt(turns[1].heard, "hello again") is True

    # The reply is said once: the final-answer item is silent and the boundary
    # carries it, so the accumulator never doubles it.
    assert turns[0].said == "audit done"
    assert turns[1].said == "hi there"

    # Exactly one final completion per turn, and each turn id is handled once.
    accumulator = TurnAccumulator()
    for event in boundaries:
        assert accumulator.already_handled(event.turn_id) is False
        accumulator.mark_handled(event.turn_id)


def test_paginated_completion_requires_its_own_task_complete():
    """No item or raw record is a completion; only task_complete ends a turn."""
    observer = CodexObserver()
    records = [
        _session_meta(),
        *_paginated_turn(
            "turn-a",
            "do the codex audit",
            "audit done",
            extra=(
                _item_completed(
                    "turn-a",
                    {
                        "type": "AgentMessage",
                        "id": "commentary-1",
                        "content": [{"type": "Text", "text": "halfway note"}],
                        "phase": "commentary",
                    },
                    started_ms=1789040003000,
                    completed_ms=1789040003100,
                ),
                _item_completed(
                    "turn-a",
                    {
                        "type": "UserMessage",
                        "id": "user-item-queued",
                        "content": [{"type": "text", "text": "queued note", "text_elements": []}],
                    },
                    started_ms=1789040003200,
                    completed_ms=1789040003300,
                ),
                _raw_message("turn-a", "user", "raw-late", "queued note", "input_text"),
            ),
        ),
    ]
    parsed = _parsed(observer, records)
    events = _events(parsed)

    assert [event.turn_id for event in events if event.turn_end] == ["turn-a"]

    # Mid-turn commentary is assistant text inside the same turn.
    assistant = [event for event in events if event.kind is EventKind.ASSISTANT]
    assert [event.text for event in assistant] == ["halfway note", "audit done"]

    turns = _accumulate(events)
    assert len(turns) == 1
    assert turns[0].said == "halfway note\n\naudit done"
    assert answers_prompt(turns[0].heard, "do the codex audit") is True
    assert answers_prompt(turns[0].heard, "queued note") is True


# ---- no duplication between the two representations -----------------------


def test_modern_and_raw_representations_do_not_duplicate_messages():
    """The trajectory keeps one canonical copy of each message.

    Control hears/says the items while the trajectory takes the raw
    ``response_item`` message facts; the paginated message items mark
    themselves control-only so projecting their events would not add a
    second USER/ASSISTANT record for the same words.
    """
    observer = CodexObserver()
    records = [_session_meta(), *_paginated_turn("turn-a", "do the audit", "audit done")]
    parsed = _parsed(observer, records)

    user_items = [
        record
        for record in parsed
        if record.trajectory == ()
        and any(
            event.kind is EventKind.USER and event.text == "do the audit" for event in record.events
        )
    ]
    assert user_items, "the UserMessage item must produce a USER control event"
    for record in parsed:
        if any(event.kind is EventKind.USER for event in record.events):
            assert record.trajectory_events == (), "USER events must not project to trajectory"

    canonical = project_events_and_facts(
        [
            event
            for record in parsed
            for event in (
                record.trajectory_events if record.trajectory_events is not None else record.events
            )
        ],
        _facts(parsed),
        participant_id="p",
        source_epoch="e",
    )
    user_records = [r for r in canonical if r.kind is TrajectoryKind.USER]
    assert len(user_records) == 1
    assert user_records[0].summary == "do the audit"
    assistant_records = [r for r in canonical if r.kind is TrajectoryKind.ASSISTANT]
    assert len(assistant_records) == 1
    assert assistant_records[0].summary == "audit done"


def test_command_execution_and_reasoning_items_stay_silent():
    """Kinds the raw response items cover emit nothing from the items."""
    observer = CodexObserver()
    exec_call = _record(
        "response_item",
        {
            "type": "custom_tool_call",
            "id": "exec-call-1",
            "call_id": "call-1",
            "name": "exec",
            "input": "tools.exec_command\npwd",
            "internal_chat_message_metadata_passthrough": {"turn_id": "turn-a"},
        },
    )
    exec_output = _record(
        "response_item",
        {
            "type": "custom_tool_call_output",
            "id": "exec-out-1",
            "call_id": "call-1",
            "output": "/repo\n",
            "internal_chat_message_metadata_passthrough": {"turn_id": "turn-a"},
        },
    )
    exec_item = _item_completed(
        "turn-a",
        {
            "type": "CommandExecution",
            "id": "exec-1",
            "command": ["/bin/zsh", "-lc", "pwd"],
            "cwd": "file:///repo",
            "status": "completed",
            "stdout": "/repo\n",
            "exit_code": 0,
            "duration": {"secs": 0, "nanos": 100},
        },
        started_ms=1789040000000,
        completed_ms=1789040000100,
    )
    reasoning_item = _item_completed(
        "turn-a",
        {
            "type": "Reasoning",
            "id": "rs_1",
            "summary_text": [],
            "raw_content": [],
        },
        started_ms=1789040000200,
        completed_ms=1789040000300,
    )
    parsed = _parsed(observer, [exec_call, exec_item, exec_output, reasoning_item])

    kinds = [(fact.kind, fact.summary) for record in parsed for fact in record.trajectory]
    tool_calls = [k for k in kinds if k[0] is TrajectoryKind.TOOL_CALL]
    tool_results = [k for k in kinds if k[0] is TrajectoryKind.TOOL_RESULT]
    assert len(tool_calls) == 1
    assert len(tool_results) == 1

    events = _events(parsed)
    call_events = [e for e in events if e.kind is EventKind.TOOL_CALL]
    result_events = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert len(call_events) == 1
    assert len(result_events) == 1


# ---- paginated-only kinds: MCP, file changes, plan, compaction ------------


def test_mcp_tool_call_item_produces_paired_events_and_facts():
    """MCP calls exist only as items in paginated history — report both sides."""
    observer = CodexObserver()
    records = [
        _session_meta(),
        _item_completed(
            "turn-a",
            _mcp_item("mcp-item-1"),
            started_ms=1789040005000,
            completed_ms=1789040007000,
        ),
    ]
    parsed = _parsed(observer, records)
    events = _events(parsed)

    call = next(e for e in events if e.kind is EventKind.TOOL_CALL)
    result = next(e for e in events if e.kind is EventKind.TOOL_RESULT)
    assert call.tool_name == "theater.whoami"
    assert call.turn_id == "turn-a"
    assert result.tool_name == "theater.whoami"
    assert result.text == "whoami: ok"

    facts = _facts(parsed)
    call_fact = next(f for f in facts if f.kind is TrajectoryKind.TOOL_CALL)
    result_fact = next(f for f in facts if f.kind is TrajectoryKind.TOOL_RESULT)
    assert call_fact.native_id == "mcp-item-1"
    assert call_fact.call_id == "mcp-item-1"
    assert call_fact.mcp_server == "theater"
    assert call_fact.mcp_tool == "whoami"
    assert call_fact.turn_id == "turn-a"
    assert result_fact.parent_call_id == "mcp-item-1"
    assert result_fact.summary == "whoami: ok"
    assert result_fact.status is TrajectoryStatus.COMPLETED
    assert result_fact.failure is None
    # Timing from the item's own stamps and duration.
    assert result_fact.timing is not None
    assert result_fact.timing.start == pytest.approx(1789040005.0)
    assert result_fact.timing.end == pytest.approx(1789040007.0)
    assert result_fact.timing.duration_ms == pytest.approx(1500.0)


def test_failed_mcp_item_reports_the_error_path():
    observer = CodexObserver()
    item = _mcp_item("mcp-item-2")
    item["status"] = "failed"
    del item["result"]
    item["error"] = {"message": "tool unavailable"}
    records = [
        _session_meta(),
        _item_completed("turn-a", item, started_ms=1789040005000, completed_ms=1789040005001),
    ]
    parsed = _parsed(observer, records)
    facts = _facts(parsed)
    result_fact = next(f for f in facts if f.kind is TrajectoryKind.TOOL_RESULT)
    assert result_fact.status is TrajectoryStatus.ERROR
    assert result_fact.summary == "tool unavailable"
    assert result_fact.failure is not None
    events = _events(parsed)
    result_event = next(e for e in events if e.kind is EventKind.TOOL_RESULT)
    assert result_event.text == "tool unavailable"


def test_file_change_item_reports_apply_patch_paths():
    observer = CodexObserver()
    records = [
        _session_meta(),
        _item_completed(
            "turn-a",
            _file_change_item("patch-1"),
            started_ms=1789040008000,
            completed_ms=1789040009000,
        ),
    ]
    parsed = _parsed(observer, records)
    events = _events(parsed)

    patch_events = [e for e in events if e.tool_name == "apply_patch"]
    assert len(patch_events) == 1
    assert patch_events[0].kind is EventKind.TOOL_CALL
    assert [(p.path, p.mode) for p in patch_events[0].paths] == [("theater/new.py", "write")]

    facts = _facts(parsed)
    call_fact = next(f for f in facts if f.kind is TrajectoryKind.TOOL_CALL)
    result_fact = next(f for f in facts if f.kind is TrajectoryKind.TOOL_RESULT)
    assert call_fact.native_id == "patch-1:call"
    assert call_fact.call_id == "patch-1"
    assert call_fact.turn_id == "turn-a"
    assert result_fact.parent_call_id == "patch-1"
    assert result_fact.summary == "patch applied"
    assert result_fact.status is TrajectoryStatus.COMPLETED
    assert call_fact.timing is not None
    assert call_fact.timing.start == pytest.approx(1789040008.0)


def test_plan_and_compaction_items_report_context_facts():
    observer = CodexObserver()
    records = [
        _session_meta(),
        _item_completed(
            "turn-a",
            {"type": "Plan", "id": "plan-1", "text": "1. read\n2. fix"},
            started_ms=1789040010000,
            completed_ms=1789040010001,
        ),
        _item_completed(
            "turn-a",
            {"type": "ContextCompaction", "id": "compact-1"},
            started_ms=1789040011000,
            completed_ms=1789040012000,
        ),
    ]
    facts = _facts(parsed := _parsed(observer, records))
    context = [f for f in facts if f.kind is TrajectoryKind.CONTEXT]
    assert [(f.summary, f.native_id, f.turn_id) for f in context] == [
        ("1. read\n2. fix", "plan-1", "turn-a"),
        ("context compacted", "compact-1", "turn-a"),
    ]
    assert parsed[1].trajectory[0].timing is not None
    assert parsed[1].trajectory[0].timing.end == pytest.approx(1789040010.001)


# ---- legacy compatibility ------------------------------------------------


def _legacy_turn(turn_id: str, prompt: str, reply: str) -> list[dict]:
    return [
        _record("event_msg", {"type": "task_started", "turn_id": turn_id}),
        _record("event_msg", {"type": "user_message", "message": prompt, "turn_id": turn_id}),
        _raw_message(turn_id, "user", f"raw-user-{turn_id}", prompt, "input_text"),
        _record(
            "event_msg",
            {"type": "agent_message", "message": reply, "phase": "final_answer"},
        ),
        _record(
            "event_msg",
            {"type": "task_complete", "turn_id": turn_id, "last_agent_message": reply},
        ),
    ]


def test_legacy_events_still_drive_the_same_control_outcome():
    """Legacy `user_message`/`agent_message` records keep their meaning."""
    observer = CodexObserver()
    legacy = [*_legacy_turn("turn-a", "do the audit", "audit done")]
    events = _events(_parsed(observer, legacy))
    turns = _accumulate(events)

    assert [e.turn_id for e in events if e.turn_end] == ["turn-a"]
    assert len(turns) == 1
    assert answers_prompt(turns[0].heard, "do the audit") is True
    assert turns[0].said == "audit done"


def test_legacy_and_paginated_equivalents_agree():
    legacy_events = _events(_parsed(CodexObserver(), [*_legacy_turn("t", "p", "r")]))
    paginated_events = _events(_parsed(CodexObserver(), [*(_paginated_turn("t", "p", "r"))]))

    def shape(events):
        # Modern items carry a native turn claim on user text that the legacy
        # records never had; the control outcome (what is heard, said, and
        # closed) is what must stay identical.
        return [
            (event.kind, event.text, event.turn_end) for event in events if not event.usage_only
        ]

    assert shape(paginated_events) == shape(legacy_events)
    assert (
        [e.turn_id for e in paginated_events if e.turn_end]
        == [e.turn_id for e in legacy_events if e.turn_end]
        == ["t"]
    )


def test_legacy_patch_and_mcp_events_still_parse():
    observer = CodexObserver()
    records = [
        _session_meta(),
        _record(
            "event_msg",
            {
                "type": "patch_apply_end",
                "call_id": "call-1",
                "turn_id": "turn-a",
                "stdout": "",
                "stderr": "",
                "success": True,
                "changes": {"a/b.py": {"type": "update", "unified_diff": "..."}},
            },
        ),
        _record(
            "event_msg",
            {
                "type": "mcp_tool_call_begin",
                "call_id": "mcp-1",
                "invocation": {"server": "theater", "tool": "whoami", "arguments": {}},
            },
        ),
        _record(
            "event_msg",
            {
                "type": "mcp_tool_call_end",
                "call_id": "mcp-1",
                "invocation": {"server": "theater", "tool": "whoami", "arguments": {}},
                "result": {"Ok": {"content": [{"type": "text", "text": "ok"}]}},
            },
        ),
    ]
    parsed = _parsed(observer, records)
    events = _events(parsed)
    assert [(e.tool_name, e.kind) for e in events] == [
        ("apply_patch", EventKind.TOOL_CALL),
        ("theater.whoami", EventKind.TOOL_CALL),
        ("theater.whoami", EventKind.TOOL_RESULT),
    ]
    facts = _facts(parsed)
    assert any(f.kind is TrajectoryKind.TOOL_CALL and f.summary == "apply_patch" for f in facts)
    mcp_result = next(
        f for f in facts if f.kind is TrajectoryKind.TOOL_RESULT and f.call_id == "mcp-1"
    )
    assert mcp_result.summary == "ok"


# ---- cold history and live projection agree ------------------------------


@pytest.mark.asyncio
async def test_cold_history_and_live_projection_agree(tmp_path: Path):
    path = tmp_path / "rollout.jsonl"

    def write(records):
        with path.open("a") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    write([_session_meta(), *_paginated_turn("turn-a", "do the audit", "audit done")])

    def observer_and_source():
        observer = CodexObserver(root=tmp_path)
        return observer, observer.open_source(
            cwd=str(tmp_path),
            known_location=str(path),
            session_provenance=TranscriptProvenance.OPERATOR,
        )

    _, cold_source = observer_and_source()
    cold_page = await cold_source.history_page(limit=50)

    _observer, live_source = observer_and_source()
    attached = await live_source.read()
    assert attached.attached is not None
    live_source.commit_attachment()

    write([*_paginated_turn("turn-b", "do the audit", "audit done")])
    batch = await live_source.read()

    def shape_of(turn_id, facts):
        return [
            (
                fact.kind,
                fact.summary,
                (fact.native_id or "").replace(turn_id, "turn"),
            )
            for fact in facts
            if fact.turn_id == turn_id
        ]

    live_shape = shape_of("turn-b", batch.trajectory)
    cold_shape = shape_of("turn-a", cold_page.trajectory)
    assert live_shape == cold_shape, (
        "cold history and live projection must agree on the same native records"
    )
    assert live_shape, "the live batch carried the appended turn"
    # The control half agrees too: the live batch heard the prompt.
    user_events = [e for e in batch.events if e.kind is EventKind.USER]
    assert [e.text for e in user_events] == ["do the audit"]


# ---- native dual-stream: raw records + rich items for one logical call ----


def _canonical(parsed):
    return project_events_and_facts(
        [
            event
            for record in parsed
            for event in (
                record.trajectory_events if record.trajectory_events is not None else record.events
            )
        ],
        _facts(parsed),
        participant_id="p",
        source_epoch="e",
    )


def _raw_tool_call(
    call_id: str,
    *,
    name: str = "apply_patch",
    input_text: str = "*** Begin Patch\n*** Add File: theater/new.py\n+ x = 1\n*** End Patch",
) -> dict:
    return _record(
        "response_item",
        {
            "type": "custom_tool_call",
            "id": f"ctc-{call_id}",
            "call_id": call_id,
            "name": name,
            "input": input_text,
        },
    )


def _raw_tool_output(call_id: str, *, output: str = "ok", record_id: str | None = None) -> dict:
    return _record(
        "response_item",
        {
            "type": "custom_tool_call_output",
            "id": record_id or f"ctco-{call_id}",
            "call_id": call_id,
            "output": output,
        },
    )


def _dual_stream_records(
    call_id: str,
    item: dict,
    *,
    output: str = "ok",
    output_record_id: str | None = None,
    item_before_output: bool = True,
    name: str = "apply_patch",
) -> list[dict]:
    """One logical tool call persisted as raw records *and* a rich item.

    Native Codex sets ``item.id = raw call_id``
    (``core/src/tools/events.rs`` ``emit_patch_end``,
    ``core/src/mcp_tool_call.rs``), and ``rollout/src/policy.rs`` persists the
    raw CustomToolCall/Output pair, so a paginated rollout can carry both
    representations of the same call. The native order is call → item →
    output (``emit_turn_item_completed`` runs before the call result lands).
    """
    item_record = _item_completed(
        "turn-a", item, started_ms=1789040005000, completed_ms=1789040007000
    )
    records = [_raw_tool_call(call_id, name=name)]
    if item_before_output:
        records.append(item_record)
        records.append(_raw_tool_output(call_id, output=output, record_id=output_record_id))
    else:
        records.append(_raw_tool_output(call_id, output=output, record_id=output_record_id))
        records.append(item_record)
    return records


def test_raw_patch_and_item_collapse_to_one_logical_call():
    """Probe: raw call + correlated FileChange item + raw output = ONE pair.

    Before the correlation the canonical store held two tool calls
    (``e:call-1`` and ``e:call-1:call``) and two tool results
    (``e:call-1:result`` and a coordinate fallback), and control saw two
    TOOL_CALL events. The item's ``id`` *is* the raw ``call_id``, so the
    item adopts the raw identity and a revision above the raw facts; the
    canonical merge keeps one call and one result, carrying the item's
    rich detail, and the raw pair keeps the single control event pair.
    """
    observer = CodexObserver()
    records = [
        _session_meta(),
        *_dual_stream_records("call-1", _file_change_item("call-1")),
    ]
    parsed = _parsed(observer, records)

    calls = [e for e in _events(parsed) if e.kind is EventKind.TOOL_CALL]
    results = [e for e in _events(parsed) if e.kind is EventKind.TOOL_RESULT]
    assert len(calls) == 1, "the raw pair is the only control source"
    assert len(results) == 1
    assert calls[0].tool_name == "apply_patch"
    assert results[0].text == "ok"

    canonical = _canonical(parsed)
    # One logical call: the item adopted the raw call's native id (they differ
    # in the raw id field — only the call_id correlation is relied on), and
    # the result claims the scoped id the item first announced.
    assert sorted(
        (r.kind, r.record_id) for r in canonical if r.kind is TrajectoryKind.TOOL_CALL
    ) == [(TrajectoryKind.TOOL_CALL, "e:ctc-call-1")]
    assert sorted(
        (r.kind, r.record_id) for r in canonical if r.kind is TrajectoryKind.TOOL_RESULT
    ) == [(TrajectoryKind.TOOL_RESULT, "e:call-1:result")]
    call_record = next(r for r in canonical if r.kind is TrajectoryKind.TOOL_CALL)
    result_record = next(r for r in canonical if r.kind is TrajectoryKind.TOOL_RESULT)
    # The surviving records are the item's rich versions, not the raw pair's.
    assert call_record.timing is not None
    assert call_record.timing.start == pytest.approx(1789040005.0)
    assert result_record.summary == "patch applied"
    assert result_record.status is TrajectoryStatus.COMPLETED
    assert result_record.failure is None
    # The rich paths survive on the fact that fed the canonical call record.
    call_fact = next(f for f in _facts(parsed) if f.kind is TrajectoryKind.TOOL_CALL)
    assert call_fact.native_id == "ctc-call-1"
    assert "theater/new.py" in json.dumps(
        [getattr(detail, "value", None) for detail in call_fact.details], default=str
    )


def test_raw_mcp_and_item_collapse_to_one_logical_call():
    observer = CodexObserver()
    records = [
        _session_meta(),
        *_dual_stream_records(
            "mcp-1",
            _mcp_item("mcp-1"),
            name="mcp__theater__whoami",
        ),
    ]
    parsed = _parsed(observer, records)

    calls = [e for e in _events(parsed) if e.kind is EventKind.TOOL_CALL]
    results = [e for e in _events(parsed) if e.kind is EventKind.TOOL_RESULT]
    assert len(calls) == 1
    assert len(results) == 1

    canonical = _canonical(parsed)
    call_records = [r for r in canonical if r.kind is TrajectoryKind.THEATER_CALL]
    result_records = [r for r in canonical if r.kind is TrajectoryKind.THEATER_RESULT]
    assert [r.record_id for r in call_records] == ["e:ctc-mcp-1"]
    assert [r.record_id for r in result_records] == ["e:mcp-1:result"]
    # MCP identity and the item's result text survive the merge.
    assert call_records[0].mcp_server == "theater"
    assert call_records[0].mcp_tool == "whoami"
    assert result_records[0].summary == "whoami completed"
    assert result_records[0].status is TrajectoryStatus.COMPLETED


def test_dual_stream_failure_path_retains_the_error():
    """A failed call keeps exactly one record pair — with its failure."""
    observer = CodexObserver()
    item = _file_change_item("call-1", status="failed")
    item["stderr"] = "patch conflict"
    records = [
        _session_meta(),
        *_dual_stream_records("call-1", item, output="patch failed"),
    ]
    parsed = _parsed(observer, records)
    canonical = _canonical(parsed)
    result_records = [r for r in canonical if r.kind is TrajectoryKind.TOOL_RESULT]
    assert [r.record_id for r in result_records] == ["e:call-1:result"]
    assert result_records[0].status is TrajectoryStatus.ERROR
    assert result_records[0].failure is not None
    assert result_records[0].failure.category is not None


def test_dual_stream_output_before_item_collapses_too():
    """Order-independence: call, output, item still yields one pair."""
    observer = CodexObserver()
    records = [
        _session_meta(),
        *_dual_stream_records(
            "call-1",
            _file_change_item("call-1"),
            output_record_id="ctco-1",
            item_before_output=False,
        ),
    ]
    parsed = _parsed(observer, records)
    canonical = _canonical(parsed)
    assert sorted(
        (r.kind, r.record_id) for r in canonical if r.kind is TrajectoryKind.TOOL_CALL
    ) == [(TrajectoryKind.TOOL_CALL, "e:ctc-call-1")]
    assert sorted(
        (r.kind, r.record_id) for r in canonical if r.kind is TrajectoryKind.TOOL_RESULT
    ) == [(TrajectoryKind.TOOL_RESULT, "e:ctco-1")]
    calls = [e for e in _events(parsed) if e.kind is EventKind.TOOL_CALL]
    results = [e for e in _events(parsed) if e.kind is EventKind.TOOL_RESULT]
    assert len(calls) == 1
    assert len(results) == 1
    # The surviving result is the item's rich version.
    result_record = next(r for r in canonical if r.kind is TrajectoryKind.TOOL_RESULT)
    assert result_record.summary == "patch applied"


@pytest.mark.asyncio
async def test_dual_stream_patch_and_mcp_agree_between_history_and_live(tmp_path: Path):
    """The correlation holds on both ingestion paths, cold and live."""
    path = tmp_path / "rollout.jsonl"

    def write(records):
        with path.open("a") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    def turn_records(turn_id: str) -> list[dict]:
        patch = _dual_stream_records(f"{turn_id}-patch", _file_change_item(f"{turn_id}-patch"))
        mcp = _dual_stream_records(f"{turn_id}-mcp", _mcp_item(f"{turn_id}-mcp"))
        return [
            _record("event_msg", {"type": "task_started", "turn_id": turn_id}),
            *patch,
            *mcp,
            _record("event_msg", {"type": "task_complete", "turn_id": turn_id}),
        ]

    write([_session_meta(), *turn_records("turn-a")])

    def source():
        observer = CodexObserver(root=tmp_path)
        return observer.open_source(
            cwd=str(tmp_path),
            known_location=str(path),
            session_provenance=TranscriptProvenance.OPERATOR,
        )

    cold_source = source()
    cold_page = await cold_source.history_page(limit=50)
    cold_canonical = project_events_and_facts(
        list(cold_page.events),
        cold_page.trajectory,
        participant_id="p",
        source_epoch="e",
    )

    live_source = source()
    attached = await live_source.read()
    assert attached.attached is not None
    live_source.commit_attachment()
    write(turn_records("turn-b"))
    batch = await live_source.read()
    live_canonical = project_events_and_facts(
        list(batch.events),
        batch.trajectory,
        participant_id="p",
        source_epoch="e",
    )

    def shape(canonical, turn_id):
        return sorted(
            (r.kind, r.record_id.replace(turn_id, "turn"), r.summary)
            for r in canonical
            if r.kind
            in (
                TrajectoryKind.TOOL_CALL,
                TrajectoryKind.TOOL_RESULT,
                TrajectoryKind.THEATER_CALL,
                TrajectoryKind.THEATER_RESULT,
            )
        )

    assert shape(cold_canonical, "turn-a") == shape(live_canonical, "turn-b")
    # Exactly one call/result pair per tool on both paths.
    assert len(shape(cold_canonical, "turn-a")) == 4
    # Control agrees: one event pair per tool.
    live_calls = [e for e in batch.events if e.kind is EventKind.TOOL_CALL]
    live_results = [e for e in batch.events if e.kind is EventKind.TOOL_RESULT]
    assert len(live_calls) == 2
    assert len(live_results) == 2
