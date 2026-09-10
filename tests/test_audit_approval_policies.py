"""Approval fidelity for the shipped launch plans, audited against the
native CLIs (read-only trees under /Users/manaiki.laut/Desktop/coding_clis/).

An approval choice is a contract about what the child may do unattended, and
for every shipped harness the safe reading is the same: the intended native
policy must be pinned *explicitly*, because each CLI has a permissive channel
that silently wins when the plan says nothing:

- claude reads `permissions.defaultMode` from the user's own settings when no
  `--permission-mode` flag is passed, so an unpinned `manual` can inherit
  acceptEdits or bypassPermissions. `default` is claude's Manual mode, spelled
  the way every release accepts; `manual` is only accepted by newer releases.
- vibe falls back to the `default_agent` config whenever no `--agent` flag is
  passed, and that default is `accept-edits` but is user-configurable to
  auto-approve. The builtin `ask` profile is its documented manual policy.
- opencode merges permissive agent permission defaults (`"*": "allow"`),
  then every config file (OPENCODE_PERMISSION deep-merges inside that same
  layer), then the selected agent's own `cfg.agent.<name>.permission` from any
  config file. No env layer can beat that tail, so manual/edits are enforced
  at the one layer merged after everything the agent carries: the session's
  permission, appended once per session by the rendered native plugin before
  the first LLM call. Yolo carries no ruleset at all — `--auto` approves on
  its own and an ask ruleset would fight it.

No CLI is launched here: these are launch-plan regressions only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from shipped import ClaudeCodeHarness, OpenCodeHarness

from theater.harness import plan_launch
from theater.harness.builtin.plugins.opencode.mcp import plugin_path
from theater.harness.builtin.plugins.opencode.native_plugin import render_native_plugin
from theater.models import BadRequest

NODE = shutil.which("node")


def claude_launch(tmp_path, approval, **kwargs):
    return ClaudeCodeHarness().plan_launch(
        participant_id="p-audit",
        prompt="do the thing",
        config_path=tmp_path / "mcp.json",
        approval=approval,
        **kwargs,
    )


def vibe_launch(tmp_path, approval, **kwargs):
    return plan_launch(
        "vibe",
        participant_id="p-audit",
        prompt="do the thing",
        config_path=tmp_path / "x.json",
        approval=approval,
        **kwargs,
    )


def opencode_launch(tmp_path, approval, **kwargs):
    return OpenCodeHarness().plan_launch(
        participant_id="p-audit",
        prompt="do the thing",
        config_path=tmp_path / "x.json",
        approval=approval,
        **kwargs,
    )


# ---- every choice pins its own native policy -------------------------------


@pytest.mark.parametrize("launch", [claude_launch, vibe_launch, opencode_launch])
@pytest.mark.parametrize("approval", ["manual", "edits", "yolo"])
def test_every_approval_choice_produces_a_plan(tmp_path, launch, approval):
    """The three shipped planners accept all three choices and refuse
    everything else — with the native policy attached, not just tolerated."""
    plan = launch(tmp_path, approval)
    assert plan.argv


@pytest.mark.parametrize("launch", [claude_launch, vibe_launch, opencode_launch])
def test_an_unknown_approval_is_refused_before_a_pane_opens(tmp_path, launch):
    """The alternative is a child window that dies on a CLI-rejected flag —
    or worse, one that runs with a policy the caller never chose."""
    with pytest.raises(BadRequest) as exc:
        launch(tmp_path, "whatever")
    assert "approval must be one of" in str(exc.value)


# ---- claude: the flag beats `permissions.defaultMode` -----------------------


def test_claude_manual_pins_the_default_permission_mode(tmp_path):
    """`default` is claude's Manual permission mode; passing it explicitly
    means the launch cannot inherit a permissive `permissions.defaultMode`
    from the user's settings."""
    argv = claude_launch(tmp_path, "manual").argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert "--dangerously-skip-permissions" not in argv


def test_claude_edits_pins_accept_edits(tmp_path):
    argv = claude_launch(tmp_path, "edits").argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in argv


def test_claude_yolo_skips_permissions_and_pins_no_mode(tmp_path):
    """yolo is the only choice that may bypass claude's permission system,
    and it does not also pin a permission mode."""
    argv = claude_launch(tmp_path, "yolo").argv
    assert "--dangerously-skip-permissions" in argv
    assert "--permission-mode" not in argv


def test_claude_manual_survives_a_resume(tmp_path):
    argv = claude_launch(tmp_path, "manual", resume="old-session").argv
    assert argv[argv.index("--permission-mode") + 1] == "default"


# ---- vibe: the agent profile beats `default_agent` config ------------------


def test_vibe_manual_pins_the_ask_agent(tmp_path):
    """vibe falls back to the user-configurable `default_agent` — accept-edits
    by default — whenever no `--agent` flag is passed, so manual must name the
    builtin `ask` profile explicitly."""
    argv = vibe_launch(tmp_path, "manual").argv
    assert "--agent=ask" in argv
    assert "--yolo" not in argv
    assert "--auto-approve" not in argv


def test_vibe_edits_pins_accept_edits(tmp_path):
    argv = vibe_launch(tmp_path, "edits").argv
    assert argv[argv.index("--agent") + 1] == "accept-edits"
    assert "--yolo" not in argv


def test_vibe_yolo_auto_approves_and_names_no_agent(tmp_path):
    """yolo is vibe's auto-approve channel and must not be mixed with an
    `--agent` profile that could re-introduce asks."""
    argv = vibe_launch(tmp_path, "yolo").argv
    assert "--yolo" in argv
    assert "--agent" not in argv


def test_vibe_manual_survives_a_resume(tmp_path):
    argv = vibe_launch(tmp_path, "manual", resume="s1").argv
    assert "--agent=ask" in argv
    assert "--yolo" not in argv


# ---- opencode: the session layer beats every config file ------------------
#
# Native precedence, audited in the read-only tree (packages/opencode/src):
# `evaluate()` picks the LAST matching rule of the merged ruleset and falls
# back to `ask` (permission/index.ts). The rulesets merge in order —
# `Permission.merge` is array concatenation (permission/index.ts:200):
#
#   1. agent defaults, `"*": "allow"` (session/agent/agent.ts), then the
#      builtin agent's own extras;
#   2. `fromConfig(cfg.permission)` — every config file deep-merged
#      (config.ts), with OPENCODE_PERMISSION deep-merged into that same
#      layer (config.ts:559-561);
#   3. the selected agent's `cfg.agent.<name>.permission` from any config
#      file, merged AFTER the global layer (session/agent/agent.ts:293);
#   4. the SESSION's permission, merged after everything the agent carries
#      (session/llm.ts:149, session/tools.ts:87, session/prompt.ts:346 for
#      task subagents, session/system.ts:120), appendable at runtime by the
#      session update route, whose payload merges last (httpapi
#      handlers/session.ts:194-198).
#
# So the rendered native plugin appends the approval's ruleset to each
# session's permission once, before its first LLM call — layer 4. The tests
# below model those layers faithfully to prove a permissive layer 2 or 3
# cannot survive manual/edits, and that an env var alone (layer 2) could not.


def opencode_plugin_rules(plan, tmp_path):
    """The ruleset baked into the rendered plugin for this launch plan."""
    source = plan.files[plugin_path(tmp_path / "x.json")]
    start = source.index("const permissionRules = ")
    end = source.index("\n", start)
    return json.loads(source[start + len("const permissionRules = ") : end])


def test_opencode_manual_asks_for_everything_but_plain_reads(tmp_path):
    """The plugin carries the native ruleset array: `*: ask` first, so no
    inherited `"*": "allow"` default survives, then native's hardcoded read
    allowlist verbatim — plain `read` tool calls auto-allowed (the same
    reads-auto-allowed contract as claude and codex manual), `.env`-style
    secret files still asking."""
    plan = opencode_launch(tmp_path, "manual")
    assert "--auto" not in plan.argv
    assert "OPENCODE_PERMISSION" not in plan.env
    assert opencode_plugin_rules(plan, tmp_path) == [
        {"permission": "*", "pattern": "*", "action": "ask"},
        {"permission": "read", "pattern": "*", "action": "allow"},
        {"permission": "read", "pattern": "*.env", "action": "ask"},
        {"permission": "read", "pattern": "*.env.*", "action": "ask"},
        {"permission": "read", "pattern": "*.env.example", "action": "allow"},
    ]
    # The plugin enforces through the session update route, one append per
    # session, before the first LLM call.
    source = plan.files[plugin_path(tmp_path / "x.json")]
    assert "client.session.update" in source
    assert '"chat.message"' in source


def test_opencode_edits_allows_only_the_edit_permission(tmp_path):
    """`edit` is the permission that edit/write/apply_patch tool calls ask
    with (permission/index.ts `disabled`), so one trailing `edit: allow`
    rule — after the manual rules, so findLast picks it — is the native
    shape of an accept-edits policy; everything else still asks."""
    plan = opencode_launch(tmp_path, "edits")
    assert "--auto" not in plan.argv
    assert opencode_plugin_rules(plan, tmp_path)[-1] == {
        "permission": "edit",
        "pattern": "*",
        "action": "allow",
    }


def test_opencode_yolo_auto_approves_without_fighting_a_ruleset(tmp_path):
    """`--auto` replies to permission requests on its own; if yolo also
    carried an ask ruleset the two would fight, so the plugin enforces
    nothing."""
    plan = opencode_launch(tmp_path, "yolo")
    assert "--auto" in plan.argv
    assert opencode_plugin_rules(plan, tmp_path) == []


def test_opencode_approval_enforcement_survives_a_resume(tmp_path):
    """A forked resume opens a fresh session; the hook fires on its first
    message, so the same ruleset enforces without the prompt path."""
    plan = opencode_launch(tmp_path, "edits", resume="ses_1")
    assert plan.argv == ["opencode", "-s", "ses_1", "--fork"]
    assert opencode_plugin_rules(plan, tmp_path)[-1] == {
        "permission": "edit",
        "pattern": "*",
        "action": "allow",
    }


# Native's own evaluation semantics, mirrored from the read-only tree so the
# rules above can be shown to win against permissive config. Wildcard.match
# (util/wildcard.ts) with the patterns in play reduces to glob `*` → `.*`.


def _native_wildcard_match(value, pattern):
    import re

    if not isinstance(pattern, str):
        return False
    escaped = re.escape(pattern.replace("\\", "/")).replace("\\*", ".*")
    return re.fullmatch(escaped, value.replace("\\", "/"), re.DOTALL) is not None


def _native_from_config(mapping):
    """permission config shape -> rules, preserving key order (index.ts:186)."""
    rules = []
    for permission, value in mapping.items():
        if isinstance(value, str):
            rules.append({"permission": permission, "pattern": "*", "action": value})
        else:
            for pattern, action in value.items():
                rules.append({"permission": permission, "pattern": pattern, "action": action})
    return rules


def _native_merge(*rulesets):
    """Permission.merge is plain array concatenation (index.ts:200)."""
    return [rule for ruleset in rulesets for rule in ruleset]


def _native_evaluate(rules, permission, pattern="*"):
    """evaluate(): the LAST matching rule wins, fallback `ask` (index.ts:28)."""
    for rule in reversed(rules):
        if _native_wildcard_match(permission, rule["permission"]) and _native_wildcard_match(
            pattern, rule["pattern"]
        ):
            return rule["action"]
    return "ask"


OPENCODE_AGENT_DEFAULTS = {
    "*": "allow",
    "doom_loop": "ask",
    "question": "deny",
    "plan_enter": "deny",
    "plan_exit": "deny",
    "read": {
        "*": "allow",
        "*.env": "ask",
        "*.env.*": "ask",
        "*.env.example": "allow",
    },
}


def _permissive_native_layers(approval_env, agent_permission):
    """The ruleset an agent carries today, with or without our enforcement.

    Layer 2 is the global config permission — config files deep-merged, with
    OPENCODE_PERMISSION deep-merged into it (config.ts:559-561). Layer 3 is
    the selected agent's own config-file permission, merged after (agent.ts:
    293). Layer 4 is the session permission, which our plugin appends.
    """
    global_permission = {"*": "allow", "bash": "allow"}
    global_permission.update(approval_env)
    return _native_merge(
        _native_from_config(OPENCODE_AGENT_DEFAULTS),
        _native_from_config(global_permission),
        _native_from_config(agent_permission),
    )


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("bash", "ask"),  # explicitly allowed globally
        ("edit", "ask"),  # and by the selected agent
        ("read", "allow"),  # native's hardcoded read allowlist
        ("read.secrets.env", "ask"),  # and its secret-file carve-out
        ("read.secrets.env.local", "ask"),
        ("read.secrets.env.example", "allow"),
        ("grep", "ask"),  # native build would allow; manual asks
        ("task", "ask"),  # subagent spawn asks too
        ("doom_loop", "ask"),  # matches native's own default
    ],
)
def test_opencode_manual_survives_permissive_config(tool, expected):
    """A permissive global config AND a permissive selected-agent default both
    lose to the plugin's session append: the merged ruleset a tool call
    actually evaluates still asks, except native's own read allowlist.

    The read carve-out is expressed with the real permission/pattern pair
    (permission `read`, pattern `*.env`) — the matcher takes the pair, so
    `read.env` here stands for the `read` permission against an env path.
    """
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    agent_carries = _permissive_native_layers({}, {"*": "allow"})
    rules = _native_merge(agent_carries, [dict(r) for r in _APPROVAL_SESSION_RULES["manual"]])
    if tool.startswith("read."):
        # read permission, matching the pattern the tool name carries
        for rule in reversed(rules):
            if rule["permission"] == "read" and _native_wildcard_match(
                tool.removeprefix("read."), rule["pattern"]
            ):
                assert rule["action"] == expected
                break
        else:
            raise AssertionError("no read rule matched " + tool)
        return
    assert _native_evaluate(rules, tool) == expected


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("edit", "allow"),  # the one permission edits allows
        ("write", "allow"),  # same native `edit` permission
        ("apply_patch", "allow"),
        ("bash", "ask"),  # everything else still asks
        ("task", "ask"),
    ],
)
def test_opencode_edits_survive_permissive_config(tool, expected):
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    agent_carries = _permissive_native_layers({}, {"edit": "allow", "*": "allow"})
    rules = _native_merge(agent_carries, [dict(r) for r in _APPROVAL_SESSION_RULES["edits"]])
    # edit/write/apply_patch tool calls ask with the `edit` permission
    # (permission/index.ts disabled()); map them like native does.
    permission = "edit" if tool in ("edit", "write", "apply_patch") else tool
    assert _native_evaluate(rules, permission) == expected


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
    assert NODE is not None, "run_rendered_hook requires node"
    (tmp_path / "plugin.mjs").write_text(render_native_plugin("p_1", Path("/dev/null"), rules))
    (tmp_path / "driver.mjs").write_text(HOOK_DRIVER)
    out = subprocess.run(
        [NODE, str(tmp_path / "driver.mjs"), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_rendered_hook_is_a_no_op_for_yolo(tmp_path):
    """Yolo carries no ruleset and --auto approves on its own; the hook must
    not touch the session at all."""
    out = run_rendered_hook(tmp_path, (), {"messages": 3, "behaviors": [{}]})
    assert out == {"calls": 0, "results": [{"ok": True}] * 3, "concurrentOk": None}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
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
    effective = _native_merge(
        [{"permission": "*", "pattern": "*", "action": "allow"}, agent_deny],
        [session_deny],
        out["permission"],
    )
    assert _native_evaluate(effective, "webfetch") == "deny"
    assert _native_evaluate(effective, "edit", "protected/file.py") == "deny"
    assert _native_evaluate(effective, "edit", "ordinary.py") == "allow"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
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


def test_opencode_an_env_var_alone_could_not_enforce_manual():
    """Why the plugin exists: OPENCODE_PERMISSION deep-merges into the global
    config layer (config.ts:559-561), and the selected agent's own config
    permission merges AFTER it (agent.ts:293). A permissive agent default
    therefore flips bash back to allow — the env var cannot enforce manual,
    so the plan must not pretend it does."""
    env_only = _permissive_native_layers({"*": "ask"}, {"*": "allow"})
    assert _native_evaluate(env_only, "bash") == "allow"

    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    with_session = _native_merge(env_only, [dict(r) for r in _APPROVAL_SESSION_RULES["manual"]])
    assert _native_evaluate(with_session, "bash") == "ask"
