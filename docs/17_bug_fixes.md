# Theater Hardening Backlog

> A refined "what should be improved" list for the theater pane-injection
> orchestration layer. Produced collaboratively by three agents — a Claude
> orchestrator, a Codex peer, and a Vibe reviewer — with the Vibe pass grounded
> against the actual tree (`theater/tmux/client.py`, `daemon/observer.py`,
> `daemon/methods.py`, `daemon/store.py`, `tmux/presence.py`, and the test suite).
>
> Naming note: these are a *hardening backlog*, not literal bug fixes, and the
> rest of `docs/` uses descriptive names. Retained as `17_bug_fixes.md` per
> request; consider renaming to `hardening_backlog.md` if it lands long-term.

## The core thesis

The terminal is the universal transport, but **message semantics — did it
arrive, exactly once, and is the reply I got the reply to *my* prompt — must
live in a layer above tmux.** A `tmux` exit code of 0 means "bytes reached the
pty," not "the composer accepted and submitted them." Almost every failure mode
below is a variant of *delivered into the wrong state, and nobody noticed.*

## Priority framing

Priority is defined by a single question, not by feature category:

- **P0 — the prompt arrived intact, exactly once, and the reply I got is the
  reply to *my* prompt.**
- **P1 — still true when a human or a second sender is in the loop.**
- **P2 — I can reconstruct what happened afterwards.**

Effort: **S** ≈ hours · **M** ≈ a day or two · **L** ≈ multi-day.

## Refined backlog

| # | Item | Problem it solves | Tier | Effort |
|---|------|-------------------|:----:|:------:|
| 1 | **Pre-flight delivery gate** — before paste, assert: pane alive · `pane_pid` matches launch epoch · `bracket_paste_flag=1` · **not in a modal** | Prevents the only *irreversible* failure: an injected `Enter` accepting an approval dialog nobody asked for. Everything else yields a recoverable wrong answer; this yields an unintended action. | **P0** | S |
| 2 | **Delivery receipt** — unify ack + correlation into one observation: a `user` record whose normalized text *contains* the sent prompt (hash stored at send). Bounded deadline (~5s): no match → fail the job `not_delivered`. | Proves transport + UI acceptance + submission in one durable, receiver-side fact. Gives exact prompt↔turn correlation, kills human-turn disclosure, and retires positional FIFO for transcript harnesses. The deadline replaces "hang until `RESCUE_TIMEOUT`, then return the last thing the agent said as if it were the answer." | **P0** | M |
| 3 | **Durable job ledger + restart reconciliation** (merge of old "message IDs" + "state machine" + "event journal") | ~70% already built: handles are `{target}#{seq}` seeded from `Store.max_send_seq()`; jobs are durable SQLite (`running/done/crashed/killed`); the `bus` table + `bus_append`/`bus_tail` exist. The genuine gap is a reconciliation pass on daemon restart for in-flight jobs. | **P0** | S–M |
| 4 | **tmux test rig / fake TUI fixture** — a small app that requests DECSET 2004, logs the exact bytes received, and can be told to open a modal; driven in real tmux | Delivery is currently hand-verified ("PARTLY VERIFIED", "`kill_pane`… has not [been run]"). This fixture is what makes #1, fencing, and every future harness plugin *testable* instead of a guess. | **P0** | M |
| 5 | **Rescue-vs-clean turn-end metric per harness** (~20 lines) | Instrument before you architect: empirically shows whether #2/#6/side-channels are worth their cost, and turns silent degradation into a signal. Cheapest high-value item on the page. | **P0** | S |
| 6 | **Modal-safe `send` precondition** — consult a harness-declared modal predicate, distinct from `Status` | `human_present` is checked in `send`, but `AWAITING_INPUT` is deliberately *not* — correct for a stuck `WORKING` pane, wrong for a modal. The multi-party face of #1. | **P1** | S |
| 7 | **Human-turn attribution** — prompt-matching so a human's keystrokes match no job | Fixes the admitted bug where a job "eats the first turn end the human produces" — today a human typing into an agent's pane can resolve a *peer's* job with the human's words (wrong answer + operator-text disclosure). Falls out of #2 for transcript harnesses. | **P1** | S |
| 8 | **Sender mutex / lease scoped to senders** — formalize the existing `Busy` check as an explicit claim | Leases answer "which sender owns this pane," which is *not* the same question as presence ("is a person here"). Keep `presence.py` minimal; do not replace presence with leases. | **P1** | S |
| 9 | **Queue-depth limits + loop detection — gated behind correlation** | The `Busy` mutex keeps at most one outstanding job per pane, which is *why* positional FIFO is safe. A naive queue deletes that invariant and creates silent cross-talk (turn N answers prompt N−1 forever). **Invariant: correlation lands before queueing, or queueing never lands.** | **P1** | M |
| 10 | **Harness format-drift canary** — per-plugin schema self-check + alarm when a participant's rescue rate crosses a threshold | Turn detection rests on the undocumented JSONL of third-party CLIs that ship weekly. A field rename silently degrades everything to `turn_end_unseen`; the system looks *slow*, not *broken*. Drift is the expected event, not the exception. | **P1** | M |
| 11 | **Delivery envelope + receiver-side trust convention** (restated from "signed provenance") | A stable visual frame around injected text plus a trust convention in the harness system prompt. Honest scope: this resists *confusion*, not malicious prompt injection — and it's the only thing that helps, since the recipient is an LLM reading a pane and cannot verify a signature. | **P2** | M |
| 12 | **Per-harness `correlation` capability flag** (`"receipt" \| "positional"`) | Cross-cutting enabler for #2/#9. Declare the capability on the plugin; don't infer it at runtime (a version bump will get inference wrong). `positional` harnesses stay depth-1 forever; the FIFO tiebreak ships *with* the queue, not before it. | **P2** | S |

## The one ordering that must not be violated

```
pre-flight   pane alive · pid matches · bracket_paste_flag=1 · not in a modal   ← ship first (#1)
in-flight    paste, then wait for a receipt with a deadline
post-flight  receipt = normalized user-record match → ack + correlation, one thing (#2)
fallback     screen delta / positional FIFO — screen-only harnesses only
```

**Correlation before queueing.** Enforced by the declared `correlation` flag
(#12), not by a note in a doc. Duplicate-prompt tiebreaks (timestamp + FIFO
among matches) are dead code until the queue exists, so they ship with #9.

## Cut, with rationale

- **Capability-scoped control ops** (cancel/approve/spawn/send) — **cut.** Same
  uid, same machine, unix socket, filesystem perms: any agent that can `send`
  can already `tmux kill-pane`. It offers accident-prevention, not a security
  boundary. Revisit only if theater gains sandboxed agents, separate identities,
  restricted tools, or crosses a machine boundary. *(Both peers concurred.)*
- **Cryptographically signed provenance** — **cut as specified.** The consumer
  is an LLM reading rendered text; a signature it cannot verify is decoration,
  and the real risk (agent-to-agent prompt injection) is not one signatures
  address. Restated as #11. Reconsider only if a harness verifies signatures
  *before* presenting sender identity via trusted out-of-band context, or for a
  pure audit trail. *(Both peers concurred.)*

## What's already substantially built (don't re-implement)

- **Correlation IDs / handles:** `{target}#{seq}`, seeded from
  `Store.max_send_seq()` on boot; covered by `test_send_seq.py` + `test_restart.py`.
- **Durable jobs & event journal:** SQLite jobs table + `bus` table with
  `bus_append`/`bus_tail` and the régie bus view.
- **Bracketed-paste delivery:** `deliver_text` (`client.py:213`) already does
  `set-buffer → paste-buffer -p → send-keys Enter` with a `bracket_paste_flag`
  check — the missing piece is *verifying the result*, not the paste itself.

---

*Discussion artifact — Claude (orchestrator) × Codex (peer) × Vibe (code-grounded reviewer), 2026-08-12.*
