"""OpenAI Codex CLI.

Launch lever
------------
`-c key=value` sets a config override, and the value is parsed as TOML, so the
MCP server is registered by writing three dotted keys inline:

    -c mcp_servers.theater.command="…"  -c mcp_servers.theater.args=["mcp",…]

Verified with `codex mcp list` inside a launched session. This is an *override*
on top of ~/.codex/config.toml rather than a replacement, so the user's own
servers survive — same policy as the other two adapters.

Approval flags are always passed in pairs (`-a` with `-s`). Codex has two
independent axes — approval policy and sandbox — and with neither flag it
inherits whatever the user put in ~/.codex/config.toml, which may well be
`never` / `danger-full-access`. Theater's approval mode is a promise to the
caller of `spawn`, so it must not be inheritable.

The first-launch trust dialog
-----------------------------
On the first launch inside a directory that is not listed under
`[projects."<path>"] trust_level = "trusted"` in ~/.codex/config.toml, codex
shows a modal asking whether you trust the directory, and nothing runs until a
human answers. Tested and unable to suppress: `-a untrusted -s read-only`,
`--dangerously-bypass-approvals-and-sandbox`, and both spellings of a
`-c projects."…".trust_level="trusted"` override. Two ways out were considered
and rejected — writing the trust entry into the user's config (Theater does not
own that file) and pointing CODEX_HOME elsewhere (loses auth.json, the user's
MCP servers, and session history). So a spawn into a fresh directory sits at the
dialog until someone answers it once. `is_idle_screen` reports that pane as
awaiting input because the trust dialog renders a `›` selection row, which
is the same glyph the idle composer uses. `screen_reading` checks the modal
markers before falling through to `is_idle_screen`, so the trust dialog
classifies as TRUST rather than PROMPT.

Transcript layout
-----------------
    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<local-ISO>-<session_id>.jsonl

Two independent traps in that name. The timestamp is *local* time while every
`timestamp` field inside the file is UTC, so the two are never comparable — time
filtering here goes through stat() and nothing else. The uuid suffix, on the
other hand, is exactly `session_meta.payload.session_id` (checked on every
transcript to hand), which makes a known session id a pure glob.

Which rollout is ours
---------------------
Codex mints its `ThreadId` internally and the public CLI accepts a session id
only on `resume` and `fork`, so a new interactive session cannot be launched
with an id we chose. Until a transcript is found, the participant therefore has
no session id at all, and discovery has nothing sharper than `session_meta.cwd`
plus a birth-time floor. Two agents in one directory both satisfy that, so the
reducer's collision guard refuses both — correctly, and at the cost of the
await and of `read_transcript`.

The exact channel is the process itself: codex holds its rollout open for the
lifetime of the session, so the file descriptors of the pane's codex process
name the transcript that belongs to it. That evidence survives a daemon
restart, is available before the agent has made a single MCP call, and changes
no Codex configuration — which is more than any of `CODEX_HOME` isolation, a
`SessionStart` hook receipt, or `_meta.threadId` on an MCP request can say.

It applies to spawned panes only. There the pane process *is* the CLI Theater
started, so the pid the registry holds names that session for as long as the
participant lives. An adopted pane runs a shell instead, and a shell outlives
what it ran: the codex under it now need not be the codex the participant was
adopted from, and no amount of counting processes can tell the difference. So
adopted panes get no proof and keep the behaviour they have always had. Giving
them proof means associating a participant with a *process* at adoption time
and keeping it — daemon state, and the daemon's to keep.

Three keys, then, in a deliberate order. A session id we were *given* — a
resume token, a launch receipt — is asked first: it names the file outright,
no second codex in the pane can confuse it, and it costs a glob instead of
three subprocesses. The process is asked next. A session id we merely *read
back* off a file comes last, behind the process, because it may itself be an
earlier guess: put it first and discovery re-derives the same wrong file
forever, with no way for proof to ever displace it.

When the process cannot be inspected — no `/proc`, no `lsof`, a rollout not
yet created, more than one open at once, or more than one codex in the pane to
choose between — discovery falls back to the cwd scan exactly as before and the
candidate is reported as heuristic. Nothing here decides what to do about that:
the reducer's guard is the one place that refuses a contested attachment, and
this adapter's job is only to say honestly how well it knows.

Proof is also offered on its own, through `proven_transcript`. A participant
bound before any of this existed carries a heuristic location that every later
poll takes before discovery is consulted, so it would stay contested for the
rest of its life; the source offers such a location to the proof channel, and
only to the proof channel, so a failed probe leaves it alone rather than
replacing it with a fresh guess.

Record shape
------------
One JSON record per line, `{timestamp, type, payload}`, discriminated on
`payload.type`. The turn boundary is `task_complete`, and its
`last_agent_message` repeats the final `agent_message` verbatim — which is why
the `final_answer` phase is dropped and the text is taken from the boundary
record instead. The observer hands the *turn-ending* event's text back to
whoever is awaiting the job (observer `_answer_turn`), so a boundary event with
no text would resolve the send with an empty result — the observer falls back
to the turn's last assistant text for exactly this shape, but a boundary that
carries its own text is better than relying on that.

`turn_aborted` (a human pressing esc) also ends the turn. It has to: 8
`task_started` records across the sampled transcripts closed as 5
`task_complete` plus 3 `turn_aborted`, and treating the aborts as non-terminal
would leave a caller awaiting a reply that is never coming.
"""

from __future__ import annotations

from .launch import CodexHarness
from .observer import CodexObserver
from .trajectory import _codex_usage

__all__ = ["CodexHarness", "CodexObserver", "_codex_usage"]
