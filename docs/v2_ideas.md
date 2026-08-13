# Theater v2 Ideas

## 1. Recall

Find relevant prior agent work before spawning or briefing a child, then recommend whether to ask a live session, brief from a dead one, or make the child read fresh.

**Problem:** Every new orchestrator starts blind, re-deriving context that may already exist in a past session on the same codebase, module, or task.

The first version should avoid treating whole sessions as the retrieval unit. Sessions drift across tasks, contain abandoned approaches, and mix corrected mistakes with final conclusions. Theater has a better internal unit: a task segment, usually a spawned or sent job with a prompt, final response, touched paths, commands, outcome, and parent session. Human surfaces can still group results by session, but agent tools should return the specific segment that matched.

**What it does:**
- Indexes derived segment metadata: git root, cwd, harness, branch, start/end HEAD, prompt text, final response, touched paths, command heads, outcome, and transcript reference
- Searches path-first, with optional text fallback over task text only
- Returns action-oriented candidates:
  - `ask`: the matching session is live and addressable, so send it the question
  - `brief`: the matching segment is dead but not stale, so the parent may read a capped briefing
  - `read-yourself`: relevant files changed since the segment, so give the child paths/transcript references instead of stale context
- Explains each result with deterministic facts such as path overlap, drift, outcome, and age

**Implementation notes:**
- Do not build a learned ranker for v2. Use hard filters and lexicographic buckets:
  - same git root first
  - path overlap count
  - drift bucket: `none`, `some`, `heavy`
  - outcome bucket: `completed`, `crashed`, `abandoned`
  - age bucket: `<1d`, `<7d`, `older`
- The `why` string should render those buckets, not invent prose relevance.
- `read_transcript` already works for live sessions; live matches should usually be `ask`, not `brief`.
- The gap is indexed dead work and stale-context detection.
- SQLite FTS5 is enough for the fallback text search; avoid embeddings until path-first retrieval is proven insufficient.

**Possible data model:**

```sql
session(id, harness, tier, cwd, git_root, branch,
        head_sha_start, head_sha_end,
        started_at, ended_at, terminal_status, parent_id,
        transcript_location, transcript_format)

segment(id, session_id, ordinal, origin,
        task_text, result_text, derived_from,
        record_start, record_end, outcome)

touch(segment_id, path, mode)
command(segment_id, argv_head, exit_code)
segment_fts(task_text)
```

`origin` is `job` when Theater has a spawn/send boundary. Otherwise the segment can be the whole session, but that should be treated as lower quality evidence.

**MCP shape:**

```text
recall_search(paths: string[], query?: string, limit?: int = 5, include_live?: bool = true)
  -> { candidates: Candidate[] }

recall_read(segment_id: string)
  -> Brief
```

`recall_search` returns metadata only. `recall_read` returns a capped, labelled briefing that the parent may choose to put into a child prompt. `spawn_session` should not gain a `resume_context` parameter in v2; prompt assembly stays explicit in the parent.

Example candidate:

```json
{
  "action": "brief",
  "segment": "seg_91f2",
  "session": "4f8a12b6c868",
  "harness": "claude",
  "live": false,
  "task": "make the observer attach without replaying history",
  "outcome": "completed",
  "evidence": {
    "paths_matched": ["theater/harness/source.py"],
    "paths_total": 3,
    "age": "2h",
    "drift": {
      "sha": "88bf960",
      "commits_behind": 0,
      "matched_paths_changed": 0
    }
  },
  "why": "touched 1 of 3 requested paths; completed; unchanged since; 2h old",
  "confidence": "high"
}
```

**Safety rules:**
- Git root is a hard privacy wall. Cross-root recall requires explicit configuration, not a query flag.
- Index references and derived facts, not payloads: no file contents, diffs, full command output, fetched pages, or MCP tool results.
- Redact credential-shaped text before writing index rows.
- Every brief includes `source_harness` and `untrusted: true`.
- Brief-derived text must not feed back into FTS or future evidence.
- Provide `theater forget` and explicitly purge FTS rows.

---

## 2. Capability Registry

Each harness declares its tools, model, and specializations so the orchestrator can make smart routing decisions.

**Problem:** `list_participants` returns harness name and status, but nothing about what each agent is actually good at.

**What it does:**
- Agents self-register capabilities on join (tools available, model tier, domain specializations)
- Orchestrator queries the registry before delegating ("send perf review to Mistral agent, security review to Claude")
- Reduces misrouted work and wasted round-trips

---

## 3. Structured Handoffs

Optional JSON schema validation on `send`/`receive` so agents exchange structured data instead of parsing prose.

**Problem:** `send` passes raw text and results come back as raw text — agents have to parse fragile natural language to extract structured findings, task lists, or verdicts.

**What it does:**
- Sender declares an expected response schema
- Theater validates the response at the tool layer and retries on mismatch (same pattern as Workflow's `schema` option)
- Enables reliable pipelines: findings → verify → synthesize without prompt-engineering the format each time

---

## 4. Session Health Metrics

Expose context window usage, token spend, and session age alongside idle/working status.

**Problem:** An orchestrator can currently see if an agent is idle or working, but not whether it's 90% through its context window or has been running for 2 hours and is likely degraded.

**What it does:**
- `list_participants` (or a new `session_health` call) returns context % used, tokens spent, time alive
- Orchestrator avoids delegating to an agent near context exhaustion
- Enables smarter load balancing across a fleet of agents

---

## 5. Dead Session Briefs

Given a past segment ID, let the parent agent inspect a bounded briefing before deciding whether to include it in a new child prompt.

**Problem:** Useful context from past sessions is inaccessible to new agents without manual copy-paste.

**What it does:**
- `recall_read(segment_id)` returns the segment task, final result, touched paths, commands, source harness, transcript location, and drift metadata
- `theater brief <segment-id>` prints exactly what would be injected
- The parent composes any briefing into the child prompt explicitly

**Non-goals for v2:**
- No automatic prompt injection at spawn time
- No background LLM summarization when a session ends
- No daemon-owned model config or API key

**Dependency:** Works best in combination with Recall (#1) to find the right segment automatically.

---

## 6. Shared Task Board

A lightweight task list visible to all participants so agents can self-assign work without the orchestrator pushing every message.

**Problem:** Large fan-outs require the orchestrator to send individual messages to each agent, creating a bottleneck and a single point of failure.

**What it does:**
- Shared task queue readable and writable by all participants
- Agents poll and self-assign pending tasks
- Orchestrator posts work items; agents claim and report results
- Reduces orchestrator involvement to scoping and synthesis rather than dispatch
