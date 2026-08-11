# Theater v2 Ideas

## 1. Session Search Engine

Search past session transcripts to find sessions with relevant context for the current orchestrator's job.

**Problem:** Every new orchestrator starts blind, re-deriving context that may already exist in a past session on the same codebase, module, or task.

**What it does:**
- Indexes dead sessions by codebase path, keywords, tool calls made, files touched
- Surfaces relevant past sessions before the orchestrator starts ("this session worked on the same auth module 3 days ago")
- Lets the orchestrator pull in a past session summary as a briefing without replaying the full transcript

**Implementation notes:**
- Transcripts are JSONL — keyword/filepath extraction alone gets far before needing embeddings
- `read_transcript` already works for live sessions; the gap is dead sessions not being indexed
- Pairs naturally with Dead Session Revival (#5)

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

## 5. Dead Session Revival

Given a past session ID, inject its summary into a new session's opening prompt automatically.

**Problem:** Useful context from past sessions is inaccessible to new agents without manual copy-paste.

**What it does:**
- Theater generates a summary when a session ends and stores it alongside the transcript
- `spawn_session` accepts an optional `resume_context` parameter (session ID or search query)
- The new session opens with the past session's summary pre-loaded as context

**Dependency:** Works best in combination with the Session Search Engine (#1) to find the right past session automatically.

---

## 6. Shared Task Board

A lightweight task list visible to all participants so agents can self-assign work without the orchestrator pushing every message.

**Problem:** Large fan-outs require the orchestrator to send individual messages to each agent, creating a bottleneck and a single point of failure.

**What it does:**
- Shared task queue readable and writable by all participants
- Agents poll and self-assign pending tasks
- Orchestrator posts work items; agents claim and report results
- Reduces orchestrator involvement to scoping and synthesis rather than dispatch
