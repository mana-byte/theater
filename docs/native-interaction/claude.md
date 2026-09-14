# Claude Code native interaction plan

## Recommendation

Keep Claude Code send and interrupt on the guarded legacy path for now. The public
surfaces reviewed expose strong lifecycle hooks and an Agent SDK that can own a
bidirectional headless session, but they do not establish an authenticated way for
Theater to submit a turn to the already-running stock terminal UI with an exact
acknowledgement and turn identity.

Run a narrow compatibility spike around **channels** and **Remote Control** before
creating a runtime. They are credible leads because current release notes describe
inbound channel notifications and remotely controlled sessions, but public product
behavior is not the same as a supported local attachment API. Do not infer or
reverse-engineer a private protocol.

The public repository was inspected at commit
[`1f6015b5d578adf79c8527443328a216d6b6a3f1`](https://github.com/anthropics/claude-code/tree/1f6015b5d578adf79c8527443328a216d6b6a3f1).
It contains plugins, examples, changelog, and packaging material; it is not the
source of the proprietary interactive CLI runtime.

## Capability mapping

| Theater capability | Claude public surface | Decision |
| --- | --- | --- |
| Session identity | Theater-selected `--session-id`, transcript path, `SessionStart`/receipt hooks | Already strong for observation; retain |
| Send new turn | Agent SDK/stream-json when SDK owns process; channel/Remote Control may receive input | Stock-TUI attachment unproven; legacy |
| Queue follow-up | Claude UI can queue user prompts; Theater cannot safely identify/cancel them through a verified API | Keep Theater queue + legacy dispatch |
| Steer active turn | SDK has real-time input semantics; stock-TUI attached control unproven | Unavailable |
| Interrupt | SDK request cancellation; stock UI uses Escape | Keep guarded legacy Escape |
| Settings | CLI/SDK launch settings exist; no verified attached-session mutation/readback | Unavailable through runtime |
| Lifecycle observation | hooks and transcript | Already supported; hooks remain enrichment only |
| Session ownership | SDK/headless process can own session | Possible separate opt-in mode, not stock-TUI adapter |

Public evidence:

- Claude Code's repository advertises the binary and plugin ecosystem in
  [`README.md`](https://github.com/anthropics/claude-code/blob/1f6015b5d578adf79c8527443328a216d6b6a3f1/README.md).
- The official plugin examples demonstrate `SessionStart` injection and `Stop` /
  `UserPromptSubmit` hooks, for example
  [`plugins/learning-output-style/README.md`](https://github.com/anthropics/claude-code/blob/1f6015b5d578adf79c8527443328a216d6b6a3f1/plugins/learning-output-style/README.md)
  and
  [`plugins/hookify/hooks/hooks.json`](https://github.com/anthropics/claude-code/blob/1f6015b5d578adf79c8527443328a216d6b6a3f1/plugins/hookify/hooks/hooks.json).
- The changelog confirms SDK UUIDs, cancellation, and bidirectional/session support,
  but calls these SDK behaviors:
  [`CHANGELOG.md:4923`](https://github.com/anthropics/claude-code/blob/1f6015b5d578adf79c8527443328a216d6b6a3f1/CHANGELOG.md#L4923-L4949).
- The changelog mentions `--channels` and inbound channel notifications:
  [`CHANGELOG.md:2062`](https://github.com/anthropics/claude-code/blob/1f6015b5d578adf79c8527443328a216d6b6a3f1/CHANGELOG.md#L2062-L2072),
  [`CHANGELOG.md:2540`](https://github.com/anthropics/claude-code/blob/1f6015b5d578adf79c8527443328a216d6b6a3f1/CHANGELOG.md#L2540-L2567).
- Remote Control is clearly a supported user feature, but its release notes describe
  service/session behavior rather than a local third-party control protocol:
  [`CHANGELOG.md:163`](https://github.com/anthropics/claude-code/blob/1f6015b5d578adf79c8527443328a216d6b6a3f1/CHANGELOG.md#L163-L176).

These references establish promising capabilities, not Theater-compatible
attachment.

## Current Theater state

- [`claude/manifest.py`](../../theater/harness/builtin/plugins/claude/manifest.py#L19)
  has no runtime and declares Escape as its legacy interrupt.
- [`claude/launch.py`](../../theater/harness/builtin/plugins/claude/launch.py#L74)
  mints `--session-id`, installs launch-local settings, and writes authenticated
  receipt hooks without modifying global user configuration.
- [`claude/hooks.py`](../../theater/harness/builtin/plugins/claude/hooks.py#L1)
  explicitly implements observation-only asynchronous tool lifecycle hooks.
- Transcript observation remains the durable source; hook correlation verifies
  session ID, transcript path, and tool-use identity in
  [`correlate_tool_hook`](../../theater/harness/builtin/plugins/claude/hooks.py#L107).
- Compatibility checks for native observation are isolated in
  [`claude/compatibility.py`](../../theater/harness/builtin/plugins/claude/compatibility.py).

Nothing in this current code should be relabeled as a native interaction transport.

## Why hooks are not a sender

`UserPromptSubmit` fires because a prompt was already submitted through some other
channel. `SessionStart` can add initial context. `Stop` can influence whether Claude
stops. These callbacks can enrich status or validate identity, but they do not by
themselves provide all of:

- an API that admits a new prompt into the current stock TUI session;
- an acknowledgement that the prompt was persisted once;
- a caller-controlled or returned turn ID;
- exact-turn steering/interrupt;
- a safe way to recover after timeout without replay.

A hook that writes to stdout, mutates a transcript, or recursively invokes another
Claude process is not an acceptable substitute. Likewise, an MCP server cannot
initiate a model turn; Claude must call the MCP tool first.

Hooks may still improve observation. Any such work belongs in the existing hook
channel and must remain fail-open for Claude execution, as documented at
[`hooks.py`](../../theater/harness/builtin/plugins/claude/hooks.py#L1).

## Phase 0 — public attachment proof

Do this as a documentation/test spike without changing manifest routing.

### Candidate A: channels

Determine from official, versioned documentation and a stock binary:

1. whether a local/custom channel plugin can deliver a message into the same
   interactive terminal session Theater launched;
2. whether the channel protocol is public and supported for third parties, rather
   than limited to named integrations or Team/Enterprise services;
3. how the channel selects the exact session and authenticates the sender;
4. whether it returns an operation/message/turn identity;
5. whether it distinguishes queued follow-up from active-turn steering;
6. whether queued channel messages can be enumerated/cancelled;
7. whether it exposes exact-turn cancellation or interruption;
8. what happens across `/clear`, compaction, resume, and Remote Control reconnect.

The fact that inbound channel notifications exist is insufficient unless every
mutation can meet Theater's acknowledgement and identity rules.

### Candidate B: Remote Control

Determine whether Anthropic documents a local client API that can attach to an
existing session. A qualifying API must not require browser automation, extraction
of private cloud credentials, or reimplementation of an unpublished protocol.

Record who owns approvals and whether both the local TUI and Theater may issue
commands. Confirm whether a remotely submitted message is visibly part of the same
local transcript and carries a stable ID.

Remote Control is a no-go if the only interface is an Anthropic-managed web/mobile
product with no supported local integration contract.

### Candidate C: Agent SDK / stream-json

Confirm the already likely result: the SDK launches/owns a headless Claude session
rather than attaching to the existing TUI process. Record its send, cancellation,
session, and message-ID semantics for a possible opt-in runtime, but do not use it to
claim stock-TUI support.

### Required proof artifact

Create `tests/fixtures/claude_native_control/README.md` containing:

- exact Claude Code version and public documentation URLs;
- selected candidate and its support/lifecycle statement;
- launch and attachment sequence;
- authentication/session-selection mechanism;
- request, acknowledgement, and event examples with secrets removed;
- the exact `native_turn_id` definition;
- behavior on busy input, session switch, timeout, duplicate operation, resume, and
  UI/user input;
- pass/fail decision for send, steer, interrupt, settings, and observation
  independently.

The proof passes only if a stock TUI remains active and Theater can drive that same
session without keystrokes or private APIs.

## Preferred architecture if a public surface passes

The concrete host depends on the qualifying API:

- use `RuntimeHost.FRONTEND` if Claude exposes a stock-TUI plugin/channel callback
  with a local public client;
- use a detached backend only if the stock `claude` TUI officially supports
  attaching to a separately launched, Theater-owned backend;
- do not make an SDK-owned headless process the default runtime.

Add the following plugin-local modules rather than placing Claude policy in the
daemon:

- `theater/harness/builtin/plugins/claude/runtime_plan.py`: exact-version probe and
  pure backend/frontend plan;
- `theater/harness/builtin/plugins/claude/runtime.py`: `HarnessRuntime`, strict
  decoding, peer/session generation, and receipts;
- `theater/harness/builtin/plugins/claude/live.py`: exact session/turn/interactions
  and terminal outcomes from the public event surface;
- `theater/harness/builtin/plugins/claude/frontend.py`: only for an official
  launch-local extension/channel bridge;
- [`claude/manifest.py`](../../theater/harness/builtin/plugins/claude/manifest.py#L19):
  a per-capability runtime declaration after conformance passes.

Reuse Theater's authenticated
[`RuntimeFrontendConnection`](../../theater/harness/contracts/runtime.py#L650) if the
adapter runs inside the TUI. Do not invent a second daemon protocol.

### Minimum hypothetical request contract

This is a Theater-side requirement, not evidence that Claude currently implements
it:

```json
{
  "method": "claude.send",
  "params": {
    "operation_id": "...",
    "native_session_id": "...",
    "prompt": "..."
  }
}
```

A successful reply must contain the same operation and session IDs plus a stable
`native_turn_id`. A “message received” acknowledgement with no turn identity can
only remain `UNKNOWN`; it cannot complete a Theater job safely.

For steer/interrupt, include `expected_native_turn_id`. The adapter must reject a
stale value before mutation, ideally through an upstream atomic guard. If the
upstream method means only “interrupt whatever is active”, leave native interrupt
disabled.

## Optional separate mode: SDK-owned Claude

The Agent SDK may support a powerful Theater-owned process with bidirectional
messages, cancellation, and structured events. Treat that as a new explicit launch
mode because it changes the user experience and ownership model:

```text
stock mode:   user <-> Claude TUI in tmux
SDK mode:     Theater <-> Claude SDK/headless process
```

Before proposing it, define where approvals render, how a human joins/interacts,
which subscription/authentication modes are supported, and how it differs from
Claude's own remote/cloud agent products. It may be useful for unattended workers,
but it does not solve the stated stock local cross-harness problem by itself.

## Tests after a passing proof

Add the smallest set that covers the proven public API:

- `tests/test_claude_native_runtime.py`: strict receipts, capabilities, exact
  identity, disconnect, timeout, duplicate operation, no replay;
- `tests/fixtures/claude_native_control/`: versioned request/event samples;
- `tests/test_claude_native_control_proof.py`: opt-in stock-binary send and terminal
  correlation;
- `tests/test_control_service.py`: native send/queue and any independently proven
  steer/interrupt behavior;
- retain
  [`tests/test_harness_claude_hooks.py`](../../tests/test_harness_claude_hooks.py)
  as observation coverage, not control coverage;
- retain
  [`tests/test_claude_receipts.py`](../../tests/test_claude_receipts.py) for durable
  session/transcript identity.

The stock test must include a human-entered prompt beside a Theater-entered prompt
and prove their identities cannot be confused. It must also exercise `/clear` or
resume, because Claude can rotate session/transcript state during normal UI use.

## Release and stop gates

Native control ships only if all are true:

- the integration API is public, documented, and versioned;
- it targets the same session displayed by the stock TUI;
- it authenticates Theater without global/user credential scraping;
- send is acknowledged with a stable exact turn identity;
- completion events carry that identity;
- steer/interrupt independently reject stale expected turns;
- uncertain delivery is never replayed or followed by Escape/send-keys;
- an exact-version compatibility probe can fail closed.

Stop if the integration requires a private Remote Control protocol, transcript
mutation, terminal scraping, fake key events, or a replacement SDK process. In that
case, continue improving Claude observation and retain the safe legacy fallback.
That is a product limitation to report honestly, not a reason to weaken Theater's
control contract.
