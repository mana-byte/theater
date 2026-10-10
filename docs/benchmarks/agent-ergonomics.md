# Agent-ergonomics changes: measured impact

Scope: the changes on `rc12/agent-ergonomics` (base `4104dca4`). They fix recurring mistakes found in 2,451 real jobs across
claude, codex, opencode, pi and vibe (2026-09-07 to 2026-10-10). Every change below has a deterministic benchmark under
`scripts/bench/` that measures a concrete before/after number. None of these benchmarks runs a live agent; they replay real history,
drive the real code in-process, or simulate the mechanism, and each row says which. What a live agent does with the new shapes is
not measured here and is called out at the end.

## Reproduce

```sh
git worktree add --detach /tmp/base 4104dca4                       # the "before" checkout
uv run python scripts/bench/<script>.py                            # after (this branch)
uv run --project /tmp/base python scripts/bench/<script>.py        # before (script from this branch, code from the base)
uv run python scripts/bench/surface_history.py 4104dca4..HEAD      # surface bytes per commit
```
Some scripts take `--base /tmp/base` or `--before <json>` instead; see each script's `--help`. Reading `theater.db` is read-only.

## 1. Context every agent pays for (MCP surface)

`scripts/bench/mcp_surface.py` measures the wire bytes of `tools/list` plus the server instructions, per toolset, as a vibe agent
receives them. First-parent history, one row per merged package:

| commit | change | tools | total B | delta B |
|---|---|---:|---:|---:|
| 4104dca4 | base | 23 | 48,929 | |
| a770218c | WP-D scratchpad keys_only, quota errors, trimmed legacy paragraph | 23 | 48,900 | -29 |
| 9cf10ee8 | keep the stop-and-report rule for an unreadable legacy entry | 23 | 48,959 | +59 |
| d23f3b92 | WP-C6 read_transcript roles, first_prompt, middle_cursor | 23 | 49,261 | +302 |
| d2b1a6fa | WP-A answer inline, quiet_s, per-client wait cap | 23 | 49,350 | +89 |
| 5b9350ed | WP-C spawn/participants (resume_state paragraph moved to a skill) | 23 | 48,986 | -364 |
| 95b105fe | INSTRUCTIONS: wait with await, follow `next` | 23 | 49,142 | +156 |
| 0affe313 | WP-B12 `send(if_busy=...)` replaces steer_session and queue_followup | 21 | 46,254 | -2,888 |
| 2633a6cb | WP-B45 kill(forget=true) | 21 | 46,351 | +97 |
| **HEAD** | | **21** | **46,351** | **-2,578 (-5.3%)** |

Packages that touch only runtime behaviour (B67, B3) add 0 bytes.

## 2. Per-change impact

"replay" = real history from `theater.db` or the analysis extracts; "in-process" = real code driven in a temp daemon; "simulated" =
a model of the mechanism with stated parameters.

### Await and handles (`await_entry.py`)
| item | metric | before | after | kind |
|---|---|---|---|---|
| A1 inline final answer | completed awaits whose entry carries the answer | 0 / 832 | 642 / 832 (77%) | replay |
| A1 | added bytes per entry, mean / p95 | 0 | 1,944 / 2,118 | replay |
| A1 | codex: next call after a finished await is read_transcript | 654 / 783 | at most 149 left (505 avoidable) | replay, upper bound |
| A1 | claude: same | 23 / 71 | at most 5 left (18 avoidable) | replay, upper bound |
| A2 `quiet_s` | liveness hint on a running entry | absent | 42 s (synthetic) | in-process |
| A3 claude wait ceiling | historical claude awaits above the 120 s auto-background threshold | 139 / 143 | 0 / 143 | replay |
| A3 | ceiling for env unset / 120000 / 360000 / 0 | none | 110 / 110 / 350 / none | in-process |
| A5 handle errors | text for `['scout','#7','7','ghost']` | `no such job(s): #7, 7, ghost, scout` | names the participant, did-you-mean `abc123#7`, says handles cannot be predicted | in-process |

### Refusals (`refusal_shape.py`)
Vibe shim: a result with `isError=true` shows the model only `The MCP tool returned an error` (verified with two vibe models and the
vibe source). Visible reason characters per refusal, vibe harness:

| refusal | before | after |
|---|---:|---:|
| send to a missing target | 0 | 138 |
| read_transcript of a missing target | 0 | 138 |
| send to an unaddressable participant | 0 | 230 |
| name taken (with the free-name suggestion) | 0 | 291 |
| invalid harness value | 0 | 228 |
| load a missing skill | 0 | 262 |

Codex (isError delivery kept): 61-129 chars before, 130-289 after (the envelope adds the code and `next`). MCP timing label
`error_type` for 12 refusals: `tool_error` x12 before, the real code after (not_found x6, not_addressable x2, name_taken x2,
bad_request x2). Unknown arguments: 0 of 81 historical vibe calls carried a hint before (`tail` x77, `key` x4 silently dropped), 81 of
81 now return `ignored_arguments`.

### Spawn and participants (`spawn_ergonomics.py`)
| item | metric | before | after | kind |
|---|---|---|---|---|
| C1 non-git cwd | repos named of 12 candidates (one a linked worktree) | 0 | 10 (+2 more) | in-process |
| C1 | repair guidance after "is not" | 24 chars | 189 chars | in-process |
| C3b invalid approval | error code / length | `internal`, 217-227 chars (schema dump) | `bad_request`, 83-88 chars naming allowed values | in-process |
| C5 | spawn result echoes model, reasoning_effort | no | yes | in-process |
| C7 | newest of 250 dead rows on the first page | no | yes | in-process |
| C7 | list_participants calls to reach newest / oldest dead row | 3 / 1 | 1 / 3 | in-process |
| C7 | `ids` with `limit` | rejected | accepted | in-process |
| C8 | name taken | holder id only | also names a free `<name>-N` | in-process |
| D2 | calls to learn children's status | 2 | 1 | in-process |
| D2 | bytes for 3 children | 977 | 375 | in-process |

### read_transcript (`transcript_paging.py`, in-process pager)
Calls needed to get past one huge tool result:
| event size | before | after |
|---|---:|---:|
| 100 KiB | 7 | 1 |
| 1.4 MB | 92 | 1 |
| 10 MB | 689 | 1 |
| first message in a 61-event history with a 1.4 MB result | 98 | 1 (`roles=["user"]`) |

Last answer (`roles=["assistant"]`): 1 call before and after, but 21 assistant events in 15,953 B instead of 4 in 16,384 B. A 10 KiB
event is returned whole in both. The collapse threshold (64 KiB) was chosen from real tool output sizes: codex n=136,870, median
1.6 KB, 0.21% over 64 KiB; claude n=10,099, none over 64 KiB.

### Transcript replay and scratchpad (`transcript_replay.py`, `scratchpad_cost.py`)
| item | metric | before | after | kind |
|---|---|---:|---:|---|
| D5 claude cost-state rewrite | events emitted, 1,000-record history, 7 rewrites | 8,000 (8.0x) | 1,000 (1.0x) | in-process |
| D5 | same, 65 rewrites (the worst observed participant had 65) | 66,000 (66x), 2.66 s | 1,000 (1.0x), 0.05 s | in-process |
| D5 | real 19 MiB claude jsonl copy, one same-length rewrite | 7,106 (2.0x), 1.77 s | 3,553 (1.0x), 0.88 s | replay |
| D1 | bytes to list a 512-entry, 620 KB namespace | 629,843 | 58,963 (`keys_only`) | in-process |
| D1 | entry-cap error | 101 chars, limit only | 352 chars: count, bytes, 5 oldest keys, delete hint | in-process |
| D1 | byte-cap error | 184 chars | 355 chars, same content | in-process |

### Lifecycle (`cleanup_replay.py`, `kill_replay.py`)
| item | metric | before | after | kind |
|---|---|---:|---:|---|
| B6 | historical failed workspace_cleanup rows | 201 | 68 (56 fixable with `--force`, 10 dirty, 2 other) | replay |
| B6 | unmerged-branch retention (133 rows) settles as | failed | succeeded | replay |
| B6 | moved-branch worktree with `--force` | refused | removed, both branches kept | in-process |
| B7 | vibe failed turn error_code | none | `turn_failed` | in-process |
| B7 | vibe failed turn result text | blank | `model_stream_failed: ReadTimeout (mistral/zai-glm-5-3)` | in-process |
| B4 | transient presence refusals (28 of 88 failed terminates) admitted, N=1000, flip latency U(0,8) s, 5 s bound | 1.0% | 61% | simulated |
| B4 | focus_events_unavailable (25) | refused, with advice that cannot work | refused at once, `next` says detach or enable focus events | in-process |
| B5 | no-terminal-presence-evidence kills that can be resolved | 0 of 23 | 23 of 23 (2 by an agent, 21 operator-only) | replay |

### Redirecting a busy child (`redirect_cost.py`)
| item | metric | before | after | kind |
|---|---|---:|---:|---|
| B1 | calls to replace a busy child's turn | 6 (interrupt, 4 polls, send) | 1 | in-process |
| B1 | historical interrupt-then-send pairs within 60 s | 452 pairs, 904 calls | 452 calls | replay |
| B1 | amend when steer is unavailable | 2 calls (refused steer, then queue) | 1 (`route=queued`) | in-process |
| B1 | redirect tool descriptions | 7,476 B in 4 tools | 4,584 B in 2 tools | surface |
| B2 | the interrupted child learns what survived | hand-written by the orchestrator | the daemon prepends the card; `replaced_by` is derived | in-process |

## 3. What is not measured
- Live agent behaviour. Whether agents actually stop reading the transcript after an await, or follow `next`, needs a before/after run
  with real agents on a build of this branch; the installed daemon in this environment is the nix build of the previous version, so
  that run needs a deploy. The upper bounds above say how much is on the table, not how much will be taken.
- The B4 recovery rate is simulated with an assumed flip-latency distribution; the 28 transient refusals it is applied to are real.
- Replays use history from one campaign-heavy period (a single orchestrator drove about 70% of codex and opencode traffic on one day).
