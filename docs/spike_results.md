# Phase 0 — spike results

Status as of 2026-08-11. Machine: macOS, both harnesses installed (`claude`, `vibe` on PATH), 33 Claude Code transcripts and 885 Vibe sessions available for offline analysis.

| Spike | Status | Verdict |
|---|---|---|
| 0.1 `$TMUX_PANE` reaches MCP server | **BLOCKED** | tmux denylisted in agent environment |
| 0.2 Claude Code transcript format | **RESOLVED** | Green — better than assumed |
| 0.3 `send-keys` into repainting TUI | **BLOCKED** | tmux denylisted |
| 0.4 tmux presence variables | **BLOCKED** | tmux denylisted |
| 0.5 MCP client request timeouts | **NOT RUN** | Needs live harness invocation + MCP config change |
| 0.6 `--resume` against a live session | **NOT RUN** | Needs two concurrent live sessions |

Both go/no-go gates (0.1, 0.3) are unresolved. **The phase-0 gate is not cleared.**

---

## 0.2 — Transcript formats — RESOLVED

Analyzed offline from existing transcripts. No harness was executed.

### Claude Code

**Path:** `~/.claude/projects/<cwd-with-slashes-as-dashes>/<sessionId>.jsonl`
Filename is exactly the session UUID. The directory is a deterministic slug of the cwd — `/Users/x/Desktop/foo` becomes `-Users-x-Desktop-foo`.

**Record types observed** (n=1103 in the largest transcript):

```
assistant  472    user 304    permission-mode 70    ai-title 70
last-prompt 69    attachment 46    file-history-snapshot 40
system 28         queue-operation 4
```

**Every content record carries the registry fields we need:**
`cwd`, `gitBranch`, `sessionId`, `version`, `timestamp`, `uuid`, `parentUuid`, `isSidechain`.

`gitBranch` varies *within* a session — branch is a per-record property, not per-session.

**End of turn is explicit.** `message.stop_reason`:

```
tool_use   444    ← turn continues
end_turn    25    ← turn complete
null         3
```

This is the cleanest possible signal and better than the spec assumed.

**Content blocks:** assistant emits `text` / `thinking` / `tool_use`; user emits `text` / `tool_result`.

**Bonus:** `isSidechain` marks Claude Code's own internal Task subagents, in the same file. Free intra-harness lineage.

### Vibe

**Path:** `~/.vibe/logs/session/session_<ts>_<short_id>/messages.jsonl`, with `meta.json` alongside and an `agents/` subdirectory for subagent transcripts.

**`meta.json` gives us, for free:** `session_id`, `parent_session_id`, `child_sessions[]` (with agent name and relative path), `environment.working_directory`, `start_time`, `end_time`, `title`.

**Records:** flat `role` of `assistant` / `user` / `tool`. Keys include `content`, `message_id`, `tool_calls`, `tool_call_id`, `tool_result`, `reasoning_content`, `injected`.

**End of turn is implicit.** There is **no `stop_reason` / `finish_reason` field**. The rule is:

> an `assistant` record with no `tool_calls` key ends the turn.

Reliable, but derived rather than declared.

### Consequences for the `Harness` interface

1. **`is_turn_end()` was correctly factored.** The two harnesses answer it by completely different means — a declared enum vs. the absence of a key. The interface survives; this is the phase-2 validation arriving early.
2. **`find_transcript()` needs no `$TMUX_PANE` for Claude Code.** cwd → slug → newest file by mtime is deterministic. Vibe needs `session_id` or newest-directory-by-mtime.
3. **Lineage has two edge kinds, which the spec missed.** Both harnesses record their *own* internal subagent trees — `isSidechain` for Claude Code, `child_sessions[]` for Vibe. The tree in the régie should distinguish Theater-spawned children from harness-internal ones. This is free depth we weren't planning on, and `init_idea_grilled.md` §5 should be amended.
4. **`meta.json.end_time` updates during a session** — a coarse liveness signal independent of the transcript tail.

### Not covered by this spike

Offline files only. Live-tailing behaviour is untested: partial line writes, flush timing, and whether `stop_reason` appears atomically with the record. Verify during phase 2.

### 0.2 addendum — what phase 2 found, including one correction

Measured across 33 Claude Code transcripts and 891 Vibe sessions while writing the adapters.

**Correction: the Claude Code cwd slug is lossy and must never be inverted.** `0.2` above says "cwd → slug → newest file by mtime is deterministic". The forward direction is, but the slug is not injective: `/Users/manaiki.laut/…` becomes `-Users-manaiki-laut-…`, so a `.` and a `-` in a path component are indistinguishable afterwards, and `_` is unverified. Reconstructing a path from a directory name is therefore wrong. The adapter instead reads the verbatim `cwd` field *inside* the records and compares that, scanning candidate directories and probing each one's first records. Verified against the real tree: a session under a slug containing `manaiki-laut` is still matched correctly from the true cwd.

**Claude Code**

- The filename stem *is* the `sessionId`. Confirmed.
- `cwd` is absent from record 0 (`permission-mode`) and first appears around index 2, so a probe has to read a few records rather than just the first. The adapter bounds this at 20 records or 256 KB per candidate file.
- **Exactly one content block per record.** 67 thinking + 130 text + 275 tool_use = 472 assistant records, exactly. A multi-block assistant message is split across records that share `message.id` and `stop_reason`.
- `stop_reason`: tool_use 444, end_turn 25, null 3.
- user `message.content` is either a bare string or a list of `tool_result` / `text` blocks.
- A `tool_result` block carries only `tool_use_id`, never the tool's name. Since `parse` is stateless per line, Claude Code tool-result events have `tool_name=None` while Vibe's carry it.
- `system` subtypes across 3417 records: turn_duration 61, away_summary 20, compact_boundary 4, api_error 7. `level` is present 11 times (info 4, error 7), and `level == "error"` identifies exactly the api_error records.
- `isSidechain` was false on all 1103 records of the largest transcript, so `native_children()` usually returns nothing in practice. The mechanism is real; the traffic is rare.

**Vibe**

- **There is no timestamp anywhere in `messages.jsonl`.** Not sometimes, not under another name — searched every key of every record. Events from this harness carry `ts=None` and the observer stamps observation time separately, which is a different quantity and is labelled as one.
- Directory suffix is the first 8 characters of `session_id`; `meta.json` is authoritative for the full id.
- `tool_calls` is *absent* on turn-ending assistant records, never null. Read defensively anyway — falsy and absent should mean the same thing.
- 43 of 891 sessions have `child_sessions`; sub-agent sessions nest under `agents/<name>_…` inside the parent directory, so globbing `session_*` at the root finds only top-level sessions.

**Live tailing, the part 0.2 could not cover**

- Partial lines happen. The observer advances its offset only past the last newline and re-reads the tail next tick; no buffer of its own, because the file already is one.
- Attaching always skips to EOF, counting skipped records so indices stay true. An adopted 3 MB transcript would otherwise replay its whole history onto the bus as if it were news.
- Truncation is *not* detectable from size alone: a file rewritten to the same length looks unchanged, and the stale offset then points into the middle of a different record. `(size, mtime_ns)` together are needed.

---

## 0.1 / 0.3 / 0.4 — BLOCKED

`tmux` is on the command denylist in the agent environment:

```
Command denied: 'tmux -V' matches denylist pattern 'tmux'
```

These three spikes are unrunnable here. Two are go/no-go gates.

### Requested procedures (for manual execution)

**0.1 — does `$TMUX_PANE` reach the MCP server process?**

```bash
mkdir -p /tmp/theater-spike && cat > /tmp/theater-spike/probe.py <<'EOF'
import json, os, pathlib
pathlib.Path("/tmp/theater-spike/env-dump.json").write_text(json.dumps(dict(os.environ), indent=2))
EOF
```

Register it as a stdio MCP server for each harness, launch each *inside tmux*, trigger any tool call, then:

```bash
grep -E 'TMUX|TERM' /tmp/theater-spike/env-dump.json
```

Expect `TMUX` and `TMUX_PANE` present. Vibe is predicted green (verified by code reading: `env=None` → full inheritance, `mcp/registry.py:300`). Claude Code is unknown and is the real test.

**0.3 — `send-keys` into a repainting TUI.** With a harness running in pane `%N`:

```bash
tmux send-keys -t %N -l 'say hello'; tmux send-keys -t %N Enter          # single line
tmux send-keys -t %N -l 'line one'; tmux send-keys -t %N Escape Enter    # multi-line
# and again while the agent is mid-tool-call
```

Record whether the prompt arrives intact, mangled, or is swallowed. If unreliable, try `load-buffer` + `paste-buffer`, which triggers bracketed paste.

**0.4 — presence variables.**

```bash
tmux display-message -p -t %N '#{pane_active} #{pane_in_mode} #{session_attached} #{cursor_x},#{cursor_y}'
```

Run attached and detached; confirm `session_attached` goes to 0 on detach.

---

## 0.5 / 0.6 — NOT RUN

Both require executing the harnesses live, which costs tokens and mutates MCP configuration. Not done unattended.

- **0.5** needs a probe MCP tool that sleeps N seconds, registered with both harnesses, bisecting the timeout ceiling. Use a project-scoped `.mcp.json` rather than touching `~/.claude.json`.
- **0.6** needs `vibe --resume <id>` against a session currently open elsewhere, then a check of `messages.jsonl` for interleaved or corrupted writes.

---

## 0.7 — MCP stdio servers do not inherit the environment — RESOLVED (red for the obvious design)

Found while building phase 1a, not planned as a spike. It invalidates an assumption recorded earlier in this document.

`mcp/client/stdio/__init__.py:127`:

```python
env=({**get_default_environment(), **server.env} if server.env is not None
     else get_default_environment())
```

and `:28-44`, `get_default_environment()` copies exactly six variables on posix: `HOME LOGNAME PATH SHELL TERM USER`. Vibe reaches this path with `env=srv.env or None` (`registry.py:302,319`), so an omitted or empty `env` block means the allowlist, not inheritance.

**Consequences**

1. `THEATER_ID` in the pane environment never reaches the MCP server. Identity must ride on argv: `theater mcp --id <id>`.
2. `TMUX_PANE` is invisible to the MCP server for the same reason. The planned *primary* adoption path in phase 1b does not work; the `register_pane` fallback is promoted to primary.
3. Per-harness levers to place that argv: `claude --mcp-config FILE`; `vibe` via `$VIBE_MCP_SERVERS`, union-merged by name so the user's own servers survive.

Recorded in `init_idea_grilled.md` §6 and implemented in `theater/harness/launch.py`.

---

## 0.1 / 0.3 — partial evidence, obtained accidentally

While verifying phase 1a end-to-end, `theater spawn vibe "say hello"` was run against the real daemon. The daemon is a subprocess and is not subject to this session's tmux denylist, so it really did call tmux. Unintended, and the resulting session was killed immediately, but the observation stands:

```
tmux new-session -d -s theater -c <cwd>        pid 63400   created
vibe say hello                                 pid 63403   running inside it
new-window -P -F '#{pane_id}'                  returned    %1
```

**What this establishes:** `ensure_session`, `new_window -d -P -F`, the argv-prompt launch path, and pane-id capture all work as written on this machine. That is the spawn half of phase 1a's exit criteria.

**What it does not establish:** nothing about `send-keys` (0.3), nothing about `capture-pane` human detection (0.4), and nothing about whether the child's MCP server actually connected back with its id — the session was killed before it finished starting. Those still need a human at a terminal.

---

## Recommendation

The picture has changed since this document was first written.

| Spike | State |
|---|---|
| 0.2 transcripts | green — spec amended (two-kind lineage); both adapters now built and verified against real transcripts, see the addendum |
| 0.7 MCP env | red for the obvious design, resolved by argv |
| 0.1 pane identity | partially green for *spawned*; adoption still open, and 0.7 rules out the intended mechanism |
| 0.3 send-keys | untouched — gates phase 5b only |
| 0.4 human presence | untouched — gates phase 5b only |
| 0.5 / 0.6 | not run |

Phase 1a is no longer blocked and is now implemented. The remaining manual verification is small and specific: start a spawned agent and confirm it appears in `theater ls` with tier `spawned` **after its MCP server has connected**, which is the one link in the chain the accident did not exercise.
