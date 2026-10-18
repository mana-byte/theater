"""The rendered OpenCode approval hook enforces once, fails closed and preserves denies."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.support.approval_rig import native_evaluate, native_merge
from theater.harness.builtin.plugins.opencode.native_plugin import render_native_plugin

pytestmark = pytest.mark.usefixtures("available_harness_binaries")


HOOK_DRIVER = """
import { TheaterSessionReceipt } from "./plugin.mjs"

const scenario = JSON.parse(process.argv[2] ?? "{}")
const calls = []
let attempt = 0

const client = {
  session: {
    get: async (request) => ({
      data: { id: request.path?.id, permission: scenario.sessionPermission ?? [] },
      response: { ok: true, status: 200 },
    }),
    update: async (request) => {
      attempt += 1
      calls.push(request)
      const spec = scenario.behaviors[Math.min(attempt - 1, scenario.behaviors.length - 1)] ?? {}
      if (spec.throw) throw new Error(spec.throw)
      const payload = request.body?.permission ?? []
      const merged = spec.ignored ? [] : [...(spec.existing ?? []), ...payload]
      return {
        data: { id: request.path?.id, permission: spec.ignored ? undefined : merged },
        error: spec.error,
        response: { ok: !spec.error, status: spec.error ? 400 : 200 },
      }
    },
  },
  app: {
    agents: async () => ({
      data: scenario.agentMissing
        ? []
        : [{ name: scenario.agentName ?? "build", permission: scenario.agentPermission ?? [] }],
      response: { ok: true, status: 200 },
    }),
  },
  mcp: { status: async () => ({ data: {} }) },
}

const hooks = await TheaterSessionReceipt({ client })
const results = []
for (let i = 0; i < scenario.messages; i++) {
  try {
    await hooks["chat.message"](
      { sessionID: scenario.sessionID ?? "ses_1" },
      { message: { agent: scenario.agentName ?? "build" } },
    )
    results.push({ ok: true })
  } catch (error) {
    results.push({ ok: false, message: String(error?.message ?? error) })
  }
}
let concurrentOk = null
if (scenario.concurrent) {
  const sid = scenario.concurrent
  const output = { message: { agent: scenario.agentName ?? "build" } }
  const a = hooks["chat.message"]({ sessionID: sid }, output)
  const b = hooks["chat.message"]({ sessionID: sid }, output)
  const settled = await Promise.allSettled([a, b])
  concurrentOk = settled.every((s) => s.status === "fulfilled")
}
const summary = { calls: calls.length, results, concurrentOk }
if (scenario.capturePermission) summary.permission = calls.at(-1)?.body?.permission
console.log(JSON.stringify(summary))
"""


def run_rendered_hook(tmp_path, rules, scenario):
    """The rendered plugin, executed by node against a scripted client.

    No CLI is launched: the plugin module only touches node builtins, and
    the SDK client is a mock that mirrors the native client's shape (error
    envelopes returned, not thrown — gen/client/client.gen.ts).
    """
    (tmp_path / "plugin.mjs").write_text(render_native_plugin("p_1", Path("/dev/null"), rules))
    (tmp_path / "driver.mjs").write_text(HOOK_DRIVER)
    out = subprocess.run(
        ["node", str(tmp_path / "driver.mjs"), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout)


@pytest.mark.requires_node
def test_rendered_hook_enforces_once_on_success(tmp_path):
    """A successful append marks the session enforced; the next message on
    the same session makes no further call."""
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    out = run_rendered_hook(
        tmp_path, _APPROVAL_SESSION_RULES["manual"], {"messages": 2, "behaviors": [{}]}
    )
    assert out == {
        "calls": 1,
        "results": [{"ok": True}, {"ok": True}],
        "concurrentOk": None,
    }


@pytest.mark.requires_node
def test_rendered_hook_fails_closed_on_a_returned_sdk_error(tmp_path):
    """The SDK client resolves error envelopes instead of throwing, so an
    HTTP 400 looks like a normal return. The hook must reject with an
    actionable message, never mark the session enforced, and retry on the
    next message instead of proceeding unapproved."""
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    out = run_rendered_hook(
        tmp_path,
        _APPROVAL_SESSION_RULES["manual"],
        {"messages": 2, "behaviors": [{"error": {"name": "BadRequestError"}}]},
    )
    assert out["calls"] == 2
    assert [r["ok"] for r in out["results"]] == [False, False]
    for result in out["results"]:
        assert "refusing to run unapproved" in result["message"]
        assert "BadRequestError" in result["message"]
        assert "400" in result["message"]


@pytest.mark.requires_node
def test_rendered_hook_fails_closed_on_a_thrown_failure(tmp_path):
    """A rejected update Promise must not be swallowed: the hook rejects,
    the session is not marked enforced, and the next message retries."""
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    out = run_rendered_hook(
        tmp_path,
        _APPROVAL_SESSION_RULES["manual"],
        {"messages": 2, "behaviors": [{"throw": "boom"}]},
    )
    assert out["calls"] == 2
    assert [r["ok"] for r in out["results"]] == [False, False]
    for result in out["results"]:
        assert "boom" in result["message"]
        assert "refusing to run this message unapproved" in result["message"]


@pytest.mark.requires_node
def test_rendered_hook_retries_after_a_failure_then_enforces(tmp_path):
    """A failed append is not remembered as enforced: the next message
    retries the update and, on success, stops calling."""
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    out = run_rendered_hook(
        tmp_path,
        _APPROVAL_SESSION_RULES["manual"],
        {"messages": 3, "behaviors": [{"throw": "boom"}, {}]},
    )
    assert out["calls"] == 2
    assert [r["ok"] for r in out["results"]] == [False, True, True]


@pytest.mark.requires_node
def test_rendered_hook_makes_one_call_for_concurrent_same_session_messages(tmp_path):
    """Two chat.message hook calls racing on one session share a single
    enforcement flight — one update, one append, both callers resolved."""
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    out = run_rendered_hook(
        tmp_path,
        _APPROVAL_SESSION_RULES["manual"],
        {"messages": 0, "concurrent": "ses_conc", "behaviors": [{}]},
    )
    assert out == {"calls": 1, "results": [], "concurrentOk": True}


@pytest.mark.requires_node
def test_rendered_hook_is_a_no_op_for_yolo(tmp_path):
    """Yolo carries no ruleset and --auto approves on its own; the hook must
    not touch the session at all."""
    out = run_rendered_hook(tmp_path, (), {"messages": 3, "behaviors": [{}]})
    assert out == {"calls": 0, "results": [{"ok": True}] * 3, "concurrentOk": None}


@pytest.mark.requires_node
def test_rendered_hook_fails_closed_when_the_update_is_ignored(tmp_path):
    """A 200 that did not apply the rules is as bad as a 400: some builds
    ignore session permission updates. The hook verifies the stored
    ruleset really ends with ours and rejects otherwise, with an
    actionable message, retrying on the next message."""
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    out = run_rendered_hook(
        tmp_path,
        _APPROVAL_SESSION_RULES["manual"],
        {"messages": 2, "behaviors": [{"ignored": True}]},
    )
    assert out["calls"] == 2
    assert [r["ok"] for r in out["results"]] == [False, False]
    for result in out["results"]:
        assert "does not end with the rules" in result["message"]
        assert "ignores session permission updates" in result["message"]


@pytest.mark.requires_node
def test_rendered_hook_preserves_agent_and_session_denies(tmp_path):
    """Approval may tighten allows, but it must never turn an existing deny into ask/allow."""
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    session_deny = {"permission": "webfetch", "pattern": "*", "action": "deny"}
    agent_deny = {"permission": "edit", "pattern": "protected/*", "action": "deny"}
    out = run_rendered_hook(
        tmp_path,
        _APPROVAL_SESSION_RULES["edits"],
        {
            "messages": 1,
            "behaviors": [{}],
            "sessionPermission": [session_deny],
            "agentPermission": [
                {"permission": "*", "pattern": "*", "action": "allow"},
                agent_deny,
            ],
            "capturePermission": True,
        },
    )
    assert out["results"] == [{"ok": True}]
    assert out["permission"][-2:] == [session_deny, agent_deny]
    effective = native_merge(
        [{"permission": "*", "pattern": "*", "action": "allow"}, agent_deny],
        [session_deny],
        out["permission"],
    )
    assert native_evaluate(effective, "webfetch") == "deny"
    assert native_evaluate(effective, "edit", "protected/file.py") == "deny"
    assert native_evaluate(effective, "edit", "ordinary.py") == "allow"


@pytest.mark.requires_node
def test_rendered_hook_fails_closed_when_effective_agent_cannot_be_verified(tmp_path):
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    out = run_rendered_hook(
        tmp_path,
        _APPROVAL_SESSION_RULES["manual"],
        {"messages": 2, "behaviors": [{}], "agentMissing": True},
    )

    assert out["calls"] == 0
    assert [item["ok"] for item in out["results"]] == [False, False]
    assert all("cannot verify effective permissions" in item["message"] for item in out["results"])
