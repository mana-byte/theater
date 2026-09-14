# Claude Code native-control proof (Phase 0)

Status: **no public attachment surface qualifies.** Claude Code send and
interrupt stay on the guarded legacy tmux path; the manifest declares no runtime
and hooks remain observation-only. This directory is the evidence record for
`docs/native-interaction/claude.md` and the executable gate is
`tests/test_claude_native_control_proof.py`.

## Inspected surface

- Stock binary: **claude 2.1.220** (`claude --version`), captured verbatim in
  `help_surface.txt`.
- Public repository commit inspected for plugins/examples/changelog:
  [`1f6015b5d578adf79c8527443328a216d6b6a3f1`](https://github.com/anthropics/claude-code/tree/1f6015b5d578adf79c8527443328a216d6b6a3f1).
- Official documentation:
  - Channels: <https://code.claude.com/docs/en/channels-reference> and
    <https://code.claude.com/docs/en/channels> (research preview, v2.1.80+).
  - Remote Control: <https://code.claude.com/docs/en/remote-control>.
  - SDK/stream-json: stock `--help` gates both to `--print`.

Machine-checkable verdicts live in `public_surface.json`; the quotes below are
from the cited official pages.

## Candidate A — channels: FAIL

A channel is an MCP server the target session spawns over stdio that emits
`notifications/claude/channel` events. It fails Theatre's gates:

- **No acknowledgement.** “Claude Code doesn't acknowledge notifications. The
  `await` on `mcp.notification()` resolves when the message is written to the
  transport, not when Claude has processed it.” A Theater `ACCEPTED` requires a
  native acknowledgement plus validated identity; this can only ever be
  `UNKNOWN` with no way to distinguish delivered from dropped.
- **Silent drop.** “If the session hasn't loaded your server as a channel, or
  the organization policy blocks it, Claude Code drops the events silently and
  returns no error to your server.”
- **No identity.** No operation id, message id, or turn id is returned to the
  sender. The exact `native_turn_id` Theatre requires does not exist in this
  surface.
- **No session selection.** Delivery follows which session spawned the channel;
  there is no caller-facing API that addresses an exact already-running session.
- **No queue semantics.** Busy events “queue into the session and are processed
  in order” and are batched into the next turn; they cannot be enumerated or
  cancelled by the sender.
- **No steer/interrupt.** The channel is notification-only inbound; there is no
  exact-turn cancellation.
- **Research preview.** Custom channels are off the Anthropic-curated allowlist
  and need `--dangerously-load-development-channels` at launch; Team/Enterprise
  orgs must enable them. Not a supported stable integration contract.
- **Research preview.** Custom channels are off the Anthropic-curated allowlist and
  need `--dangerously-load-development-channels` at launch; Team/Enterprise
  orgs must enable them. `--channels` is not even part of the documented 2.1.220
  CLI surface (`help_surface.txt`): it is a hidden flag whose entries must be
  tagged `plugin:<name>@<marketplace>` (allowlist enforced) or `server:<name>`.
  Not a supported stable integration contract.

## Candidate B — Remote Control: FAIL

Remote Control officially connects claude.ai/code and the Claude iOS/Android
apps to a local session. The local CLI surface (`claude remote-control`,
`--remote-control`, `/remote-control`) is host-only: the session “makes outbound
HTTPS requests only” to the Anthropic API, requires an eligible claude.ai
subscription login, and has no documented local third-party attach API. The
stock 2.1.220 CLI has no attach/connect subcommand for interactive sessions
(see `help_surface.txt`). Reverse-engineering the private web endpoints is out
of bounds by plan.

## Candidate C — Agent SDK / stream-json: not stock-TUI

Confirmed as expected: `--input-format stream-json` and `--output-format
stream-json` are documented “(only works with --print)” on the stock binary, and
the SDK launches and owns a headless process. It cannot attach to the
already-running stock TUI and remains a possible separate opt-in mode, not a
stock-TUI adapter.

## `native_turn_id`

**None.** No inspected public surface returns a caller-addressable turn id.
Transcript message UUIDs are observation-only and are never returned by a
control API. Without one, `ACCEPTED` cannot be distinguished from `UNKNOWN`,
and steer/interrupt cannot reject a stale expected turn.

## Behavior matrix (all `UNKNOWN`-unsafe)

- busy input: events queue invisibly, batched next turn, no enumeration/cancel;
- session switch: no caller-facing session addressing at all;
- timeout/duplicate: no acknowledgement, no operation id, no receipt cache —
  a duplicate emit silently double-submits;
- resume / `/clear`: channel notifications carry no identity to correlate with;
- UI/user input: events and human keystrokes interleave in the same session with
  no attribution Theatre can verify.

## Pass/fail per capability

| Capability | Decision | Reason |
| --- | --- | --- |
| send | fail | no acknowledged, identity-bearing admission into the stock TUI |
| queue follow-up | fail | Theater queue + legacy dispatch retained; no native queue API |
| steer | fail | no turn id; no active-turn amendment surface |
| interrupt | fail | no exact-turn cancellation; guarded legacy Escape retained |
| settings update | fail | no attached-session mutation or readback API |
| observation | retained | transcript + observation-only hooks stay as shipped |

## Standing decision

Keep `claude/manifest.py` without a runtime declaration, interrupt on the
guarded legacy Escape plan, and hooks observation-only. The opt-in probe in
`tests/test_claude_native_control_proof.py` re-runs the stock-binary checks:
when the invariants above drift (for example a documented attach command or an
acknowledged send appears), the probe fails and this Phase 0 spike must be
re-run before any runtime is implemented.
