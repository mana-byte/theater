"""Pending input evidence is scoped, bounded, and independent of control state."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from theater.harness.builtin.plugins.opencode.frontend_inputs import TUI_INPUTS
from theater.harness.builtin.plugins.opencode.inputs import PendingInputs
from theater.harness.builtin.plugins.opencode.live import OpenCodeTuiLiveSource
from theater.harness.builtin.plugins.opencode.server_live import OpenCodeServerLiveSource
from theater.harness.contracts.runtime import RuntimeExecutionState, RuntimeNotification
from theater.models import Status


def test_tui_child_dialog_counts_are_scoped_without_polling_http():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the OpenCode TUI input observer")
    result = subprocess.run(
        [node, "--input-type=module"],
        input=TUI_INPUTS
        + """
import assert from "node:assert/strict"
const handlers = new Map()
const calls = []
const sessions = new Map([
  ["root", { id: "root" }],
  ["child", { id: "child", parentID: "root" }],
  ["foreign", { id: "foreign", parentID: "elsewhere" }],
])
const questions = new Map([["child", [{}]], ["foreign", [{}]]])
const observer = observeInputs({
  event: { on: (type, handler) => handlers.set(type, handler) },
  state: { session: {
    get: (id) => sessions.get(id),
    permission: () => [],
    question: (id) => questions.get(id) ?? [],
  } },
  client: { session: { children: (params, options) => new Promise((resolve) =>
    calls.push({ params, options, resolve }),
  ) } },
}, () => {})
const flush = () => new Promise((resolve) => setImmediate(resolve))
assert.equal(observer.counts("root", 1).question_count, 0)
assert.deepEqual(calls[0].params, { sessionID: "root" })
calls[0].resolve({ data: [...sessions.values()] })
await flush()
assert.equal(observer.counts("root", 1).question_count, 1)
questions.set("new-child", [{}])
handlers.get("question.asked")({ properties: { sessionID: "new-child" } })
assert.equal(observer.counts("root", 1).question_count, 1)
sessions.set("new-child", { id: "new-child", parentID: "root" })
assert.equal(observer.counts("root", 1).question_count, 2)
questions.delete("child")
handlers.get("question.replied")({ properties: { sessionID: "child" } })
assert.equal(observer.counts("root", 1).question_count, 1)
assert.equal(calls.length, 1)

observer.counts("elsewhere", 2)
observer.counts("root", 3)
assert.ok(calls[1].options.signal.aborted)
calls[1].resolve({ data: [...sessions.values()] })
await flush()
assert.equal(observer.counts("root", 3).question_count, 0)
calls[2].resolve({ data: [...sessions.values()] })
await flush()
assert.equal(observer.counts("root", 3).question_count, 1)
assert.equal(observer.counts("new-child", 4).question_count, 0)
observer.dispose()
assert.ok(calls[3].options.signal.aborted)
calls[3].resolve({ data: [] })
await flush()
""",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_pending_input_readback_cannot_resurrect_replies_or_retain_payloads():
    pending = PendingInputs()
    pending.reset("ses-1")

    def feed(event_type, properties):
        pending.feed(event_type, {"sessionID": "ses-1", **properties})

    baseline = pending.revision
    feed("question.replied", {"requestID": "q1"})
    snapshot = {
        "permission": [],
        "question": [{"id": "q1", "sessionID": "ses-1"}],
        "children": [],
    }
    assert not pending.reconcile(snapshot, session_id="ses-1", revision=baseline)
    assert not pending.awaiting
    feed("question.asked", {"id": "q1", "questions": ["private text"]})
    assert pending._requests == {("ses-1", "question", "q1")}
    for invalid in (
        {"permission": []},
        {"children": [], "permission": [], "question": [{"id": "q1"}]},
    ):
        pending.reconcile(invalid, session_id="ses-1", revision=pending.revision)
        assert pending.awaiting
    feed("question.rejected", {"requestID": "q1"})
    assert not pending.awaiting
    for number in range(200):
        feed("permission.asked", {"id": f"p{number}"})
    assert len(pending._requests) == 128
    assert pending.awaiting
    pending.reconcile(
        {"children": [], "permission": [{"id": "foreign", "sessionID": "ses-2"}], "question": []},
        session_id="ses-1",
        revision=pending.revision,
    )
    assert not pending.awaiting


@pytest.mark.parametrize("ending", ["question.replied", "question.rejected", "session.idle"])
async def test_server_counts_only_verified_direct_child_dialogs(ending):
    source = OpenCodeServerLiveSource()
    source.adopt("root", RuntimeExecutionState.ACTIVE)
    source.connected()
    snapshot = {
        "children": [
            {"id": "child", "parentID": "root"},
            {"id": "grandchild", "parentID": "child"},
            {"id": "other", "parentID": "elsewhere"},
        ],
        "permission": [],
        "question": [{"id": "q", "sessionID": sid} for sid in ("child", "grandchild", "other")],
    }
    baseline = source.pending_inputs.revision
    source.feed({"type": "session.idle", "properties": {"sessionID": "child"}})
    source.reconcile_inputs(snapshot, session_id="root", revision=baseline)
    assert not source.pending_inputs.awaiting
    source.reconcile_inputs(snapshot, session_id="root", revision=source.pending_inputs.revision)
    source.feed({"type": "session.idle", "properties": {"sessionID": "root"}})
    assert (await source.read()).status is Status.AWAITING_INPUT
    assert source.current_execution_state() is RuntimeExecutionState.IDLE
    source.feed({"type": ending, "properties": {"sessionID": "child", "requestID": "q"}})
    assert (await source.read()).status is Status.IDLE


@pytest.mark.parametrize("kind", ["permission", "question"])
async def test_server_pending_inputs_survive_busy_events_and_clear_on_reply(kind):
    source = OpenCodeServerLiveSource()
    source.adopt("ses-1", RuntimeExecutionState.ACTIVE)
    source.connected()
    source.feed({"type": f"{kind}.asked", "properties": {"sessionID": "other", "id": "r0"}})
    assert (await source.read()).status is Status.WORKING
    for request_id in ("r1", "r2"):
        source.feed(
            {"type": f"{kind}.asked", "properties": {"sessionID": "ses-1", "id": request_id}}
        )
    source.feed(
        {"type": "session.status", "properties": {"sessionID": "ses-1", "status": {"type": "busy"}}}
    )
    batch = await source.read()
    assert batch.status is Status.AWAITING_INPUT and batch.progressed
    assert source.current_execution_state() is RuntimeExecutionState.ACTIVE
    assert batch.terminal_evidence == ()
    assert not (await source.read()).progressed
    for request_id, expected in (("r1", Status.AWAITING_INPUT), ("r2", Status.WORKING)):
        source.feed(
            {
                "type": f"{kind}.replied",
                "properties": {"sessionID": "ses-1", "requestID": request_id},
            }
        )
        assert (await source.read()).status is expected
    source.feed({"type": f"{kind}.asked", "properties": {"sessionID": "ses-1", "id": "r3"}})
    source.feed({"type": "session.idle", "properties": {"sessionID": "ses-1"}})
    assert (await source.read()).status is Status.IDLE
    source.stream_lost()
    assert (await source.read()).status is None
    source.adopt("ses-2", RuntimeExecutionState.ACTIVE)
    assert not source.pending_inputs.awaiting


@pytest.mark.parametrize("kind", ["permission", "question"])
async def test_tui_pending_counts_are_display_only_and_clear_on_snapshot(kind):
    source = OpenCodeTuiLiveSource(lambda: "ses-1")
    scope = {"session_id": "ses-1", "route_session_id": "ses-1", "session_epoch": 1}
    params = {**scope, "status": {"type": "busy"}, "permission_count": 0, "question_count": 0}
    source.feed(RuntimeNotification(method="snapshot", params={**params, f"{kind}_count": 1}))
    batch = await source.read()
    assert batch.status is Status.AWAITING_INPUT and batch.progressed
    assert source.current_execution_state() is RuntimeExecutionState.ACTIVE
    source.feed(
        RuntimeNotification(
            method="event",
            params={
                **scope,
                "event": {
                    "type": "session.status",
                    "properties": {"sessionID": "ses-1", "status": {"type": "busy"}},
                },
            },
        )
    )
    assert (await source.read()).status is Status.AWAITING_INPUT
    for count in (0, -1, True, "1"):
        source.feed(
            RuntimeNotification(method="snapshot", params={**params, f"{kind}_count": count})
        )
        assert (await source.read()).status is Status.WORKING
    source.feed(RuntimeNotification(method="snapshot", params={**params, f"{kind}_count": 1}))
    source.feed(RuntimeNotification(method="snapshot", params={**params, "session_epoch": 2}))
    assert (await source.read()).status is Status.WORKING
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**params, "session_epoch": 2, "status": None, f"{kind}_count": 1},
        )
    )
    assert (await source.read()).status is Status.AWAITING_INPUT
    assert source.current_execution_state() is RuntimeExecutionState.UNKNOWN
    source.disconnected()
    assert (await source.read()).status is None
