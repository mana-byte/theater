# Trajectory ideas

Trajectory should become Theater's agent debugger and flight recorder. Its value is not showing
another transcript: it should explain what an agent is doing, what slowed or broke it, what it
used, and how work moved between participants.

This document records product ideas, not an implementation contract. Existing invariants in
`AGENTS.md` and `docs/architecture.md` remain authoritative.

## Questions trajectory should answer

- What is this participant doing now?
- What request, model call, or tool call is running, stalled, retrying, or failed?
- Where did time, tokens, and cost go?
- Which context or tool result contributed to the next model response?
- What did this participant send to or receive from another participant?
- Can the user navigate from a parent action to the exact related child action?
- Is the view complete, delayed, truncated, or limited by the harness?

## Product direction

Treat trajectory as structured execution data with a visual debugger on top. Keep the transcript
as one source of facts, but do not let transcript-shaped rows define the interface.

Theater's distinctive feature should be cross-agent causality. A delegation should be navigable as
one chain rather than several unrelated timestamped messages:

```text
parent send -> child receive/spawn -> child model and tools
            -> child reply -> parent await completed
```

Never infer hidden reasoning or chain-of-thought. Show reasoning only when a harness explicitly
records a user-visible reasoning field. Missing data must remain visibly missing.

## Backend ideas

### First-class requests and steps

Introduce a request-level projection above individual records. A request should carry, when the
source provides it:

- stable request, turn, and step identifiers;
- participant, harness, provider, and model;
- queued, running, completed, failed, cancelled, and interrupted status;
- start, first-token, and end times;
- retry count and typed failure details;
- input, output, cache, and reasoning-token usage;
- cost and whether it was reported or estimated;
- links to its context, model, tool, and coordination records.

This enables useful summaries without reconstructing a request from display rows in régie.

### Pair tool lifecycles

Project a tool invocation and its result as one logical operation, keyed by source epoch and call
ID. Preserve their original records, but expose a paired view containing:

- tool name and bounded arguments preview;
- running/completed/failed status;
- start, end, and duration;
- bounded result or error preview;
- retry or repeated-call information;
- parent request and child-call relationships.

Unmatched calls and results must remain visible and explicitly marked rather than guessed together.

### Exact cross-participant causality

Extend coordination projections with stable correlation keys and exact target records where known.
Support links for spawn, resume, send, receive, await, reply, kill, and failure. A link should open
the matching participant and event, not merely its latest trajectory.

Where exact linkage is unavailable, say so. Timestamp proximity is useful for ordering but is not
proof of causality.

### Diagnostic overview

Provide a bounded daemon-side overview for each participant:

- current request or operation;
- time in current state;
- latest error and retry state;
- totals for requests, tools, tokens, cost, and active duration;
- slowest model and tool operations in the loaded scope;
- transcript/session coverage and known gaps;
- live, stale, interrupted, unavailable, or unsupported state.

The overview should be derived once in the daemon. Régie should render it, not rescan every loaded
record after each update.

### Better timing and usage facts

Capture time-to-first-token, generation duration, and output throughput when explicitly supported.
Keep timestamps and durations honest about provenance. Aggregate usage by request, model, tool, and
participant, while distinguishing reported values from estimates.

### Typed failures and retries

Normalize enough failure metadata to answer whether an operation failed because of a provider,
tool, user cancellation, Theater, transport, timeout, or incomplete transcript. Preserve bounded
provider details for inspection. Retries should link to the operation they retry.

### Capability and coverage reporting

Each harness should report which trajectory facts it can provide: requests, models, tools, usage,
timing, reasoning, context, retries, and live updates. Coverage belongs in responses so the UI can
distinguish "no activity" from "this harness cannot expose that activity."

All harnesses retain the baseline normalized event projection. Richer plugins add facts through
the existing additive trajectory seam; policy must not move into the observation reducer.

## Frontend ideas

### Current-state strip

Put a compact status strip above the timeline. It should answer the first question immediately:

```text
Running tool · pytest tests/test_rpc.py · 8.4s
```

Include model, elapsed time, retry/error state, and live/stale coverage only when relevant. This
must update in place without rebuilding the full ledger.

### Request and step structure

Group records under clear request/step headers showing model, status, duration, token usage, and
cost. Keep turns as timeline delimiters rather than collapsible ledger noise. The structure should
make a model request and its dependent tools readable as one execution unit.

### Combined tool rows

Render a paired tool call/result as one expandable row. The collapsed form should show tool,
status, duration, and a short outcome. Expansion should appear directly below the row and expose
bounded arguments, output, and errors. Running calls update in place.

### Quick diagnostic views

Add inexpensive views over the same loaded records:

- Running — active or incomplete work;
- Errors — failures, interruptions, and retry chains;
- Slow — longest model and tool operations;
- Tools — grouped tool activity and outcomes;
- Coordination — sends, spawns, awaits, replies, and kills.

These should be filters or projections, not separately fetched copies of the trajectory.

### Contextual inspector

Use details appropriate to the selected object:

- Request/model: output, timing, usage, model, retry, and context summary.
- Tool: arguments, result, timing, error, and parent request.
- Context: source, bounded preview, size, and associated request.
- Theater: coordination payload, direction, status, and participant links.

Keep all content bounded. Régie must never offer to load an unlimited transcript or tool result.

### Causal navigation

Participant and record links should move the tree selection, stage the target trajectory, and
select the exact linked event. Provide an obvious way back to the originating event. This turns
the trajectory from a log browser into a debugger for agent orchestration.

### Honest empty and partial states

Use distinct states for:

- no activity recorded;
- waiting for the first live record;
- unsupported capability;
- source unavailable or untrusted;
- incomplete historical coverage;
- stale data after refresh failure;
- transcript identity loss with retry available in context.

Do not represent all of these as an empty ledger.

## Performance rules

- Interaction feedback must be immediate; avoid decorative animation.
- Patch changed widgets and records in place instead of remounting the panel.
- Keep hover work local and bounded; never parse or format large payloads on pointer movement.
- Compute indexes, pairings, aggregates, and search text when data enters state.
- Preserve pagination, bounded previews, byte caps, and cache limits.
- Follow the live tail only when the user is already at the tail.
- Move slow transcript/database work off the event loop.
- Do not add resize, zoom, or drag interactions unless profiling proves they stay responsive.

## Suggested delivery order

1. Add capability/coverage reporting and a current-state overview.
2. Add first-class request projection and request headers.
3. Pair tool calls/results and render combined expandable rows.
4. Add Running, Errors, Slow, Tools, and Coordination views.
5. Add exact cross-participant record links and back navigation.
6. Add richer model/context inspectors and timing/usage diagnostics.

Each phase should remain useful with baseline harness data. Rich adapters can improve fidelity
incrementally without making trajectory exclusive to one harness.

## Implementation status

The shared trajectory layer now implements the full delivery order:

- capability and coverage reporting, current operation, loaded-scope totals, active duration,
  slowest operations, errors, and explicit retry counts;
- first-class request projections with provider/model, usage, cost provenance, timing diagnostics,
  typed failures, retry links, and exact retained-record associations;
- paired tool operations with one expandable row, bounded input/result previews, typed failures,
  retry links, timing, and unmatched-call states;
- All, Running, Errors, Slow, Tools, and Coordination views over the same cached records;
- exact Theater bus correlations, bounded daemon lookup, cross-participant navigation, and bounded
  in-memory back history;
- contextual model, request, tool, context, and Theater inspection with clickable record and
  participant links.

Fidelity remains source-dependent. Every built-in plugin keeps native keys and parsing rules in
its own monolithic module and emits only normalized contract values. No current built-in claims
retry support, so retry UI and aggregation remain dormant until a plugin supplies an explicit
native retry link. Missing provider, first-token, failure, cost, or causal data remains unknown;
Theater does not infer it.

## Explicit non-goals for now

- Reproducing DeepSeek's browser UI exactly.
- Displaying or inferring private chain-of-thought.
- Loading full unbounded transcript, context, or tool payloads in régie.
- Persisting runtime UI state or trajectory cache as a second source of truth.
- Wheel zoom, range dragging, right-click panning, resizable inspectors, or global turn folding.
- Moving transcript reads, SQLite writes, tmux mutation, or canonical identity resolution into
  régie.
