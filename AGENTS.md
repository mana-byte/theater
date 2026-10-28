# AGENTS.md

Guidance for AI agents working in this repository. Human-facing usage lives in
[`README.md`](README.md); the deep rationale lives in [`docs/architecture.md`](docs/architecture.md).
Code establishes current behaviour; documented invariants establish intended constraints.
Investigate and report disagreements instead of treating an invariant violation as a convention.

## What this is

**Theater** is a provider-backed orchestration layer for coding-agent CLIs
(Claude Code, Codex, opencode, Pi, Vibe). Agents in different harnesses discover
each other, delegate work, and await results without knowing what the others
are. Its core has two process types, with frontends and terminal providers as
independent clients:

- **daemon** — one per machine, owns all orchestration state (SQLite), the unix
  socket, launch planning, and control policy; it never executes tmux itself.
- **MCP server** — one short-lived stdio process per agent; a thin client that
  forwards and never touches SQLite or terminals directly.
- **Régie** — a separately packaged Textual frontend and persistent tmux terminal
  provider; both roles use only Theater's public frontend API.

Python 3.12+. CLI entry point: `theater.cli:main`.

## The one constraint

**MCP has no server-initiated turn.** A server cannot wake an agent. This shapes
everything:

- MCP carries *outbound* only (agent → Theater). A verified terminal-provider or
  native-runtime route carries *inbound* control. Origin alone never makes a
  participant addressable.
- Replies come back as the return value of `await_sessions`, not a callback.
- The daemon reads transcripts off disk rather than asking agents, because an
  agent mid-tool-call makes no MCP calls — exactly when you want its status.

Before changing anything in origin/addressability, `await`, or terminal routing,
know that they are load-bearing *because* of this constraint.

## Dev commands

Run from the repo root. This project uses `uv`. The full test suite is run via
the Makefile only — never bare `pytest`:

```sh
make test                     # full suite, parallel across cores (pytest-xdist)
make test-unit                # inner dev loop: -m "not integration" (real-binary proofs excluded)
make test-serial              # full suite in one process (flake debugging)
uv run pytest tests/daemon/controls # one area (see tests/README.md)
PYTEST_ADDOPTS=--cov make test # full suite with the 80% coverage gate
uv run ruff check             # lint
uv run ruff format --check    # verify formatting; omit --check to format
uv run mypy                   # Theater, Régie, compat scripts; no --strict
```

Schema changes (see architecture §5):

```sh
uv run alembic revision --autogenerate -m "add a column"
uv run alembic check          # fails if schema.py and versions/ disagree; CI runs this
```

Nix: `nix develop` gives a dev shell with the dev group plus real tmux and git.
Tests marked `tmux` drive a real tmux server through the standalone Régie
provider and self-skip when tmux is absent, so that provider goes **untested**
in a sandbox without it. Verify tmux-facing changes by hand or under
`nix develop`.

## Layout

- `theater/daemon/` — orchestration policy, SQLite persistence, controls, observation.
- `theater/mcp/`, `theater/cli/` — agent and human entry points.
- `theater/frontend/` — public SDK, DTOs, wire protocol, schemas.
- `theater/harness/` — contracts, loading, channels, normalization, transcript sources.
- `theater/harness/builtin/plugins/<harness>/` — all shipped harness-specific code.
- `theater/trajectory/` — bounded harness-neutral records and values.
- `theater/observability/` — process-level logging, metrics, tracing.
- `theater/skills/`, `theater/mcp_plugins/` — declarative skills and MCP plugins.
- `packages/regie/src/regie/` — Textual frontend and tmux terminal provider.

See the [module map](docs/architecture.md#11-module-map) for detail and
[tests/README.md](tests/README.md) for test placement.

## Code quality

- **Search before writing.** Look for existing implementations, helpers, contracts,
  and tests before adding code. Reuse or extend the existing owner when its
  semantics fit; avoid borrowing another entry point's private helpers.
- **Keep one source of truth.** Do not duplicate domain rules, validation, parsing,
  or constants. Shared logic belongs in its owning package, behind a supported
  interface. Share only when semantics match; check callers, inheritance, dispatch,
  and compatibility before merging implementations or deleting redundant wrappers.
- **Keep responsibilities focused.** Each module, class, and function has a clear
  purpose. Separate decisions from I/O where practical. Split when responsibilities
  or dependencies diverge; avoid arbitrary size limits and unnecessary fragmentation.
- **Choose the simplest clear implementation.** Avoid speculative abstractions,
  unnecessary wrappers, and configuration without a concrete requirement. Reuse
  must reduce complexity; similar-looking code alone does not justify coupling
  independent concerns. Keep compatibility facades as thin re-exports.
- **Be concise and readable.** Use precise domain names, straightforward control
  flow, and focused functions. Avoid boilerplate, redundant state, and clever
  compression that makes code harder to understand.
- **Comments explain why.** Keep comments and docstrings short; one line is the
  target. Document invariants, surprising decisions, and necessary tradeoffs.
  Do not narrate obvious code or repeat names and types. Preserve essential
  correctness explanations.
- **Review your own diff.** Remove duplication, dead code, and unnecessary
  abstractions introduced by the change. Keep unrelated cleanup separate.
  Verify changed behaviour with focused checks and report what was checked.

## Conventions

- Preserve French accents and em-dashes (`RUF001–003` disabled).
- Error classes match their wire codes: `Busy` ⇒ `busy`, `HumanPresent` ⇒
  `human_present` (`N818` disabled). Renaming one changes the caller's contract.
- Long inline errors are deliberate: tell the caller what to do (`TRY003` disabled).
- Function-local imports support lazy harness loading and keep Theater independent
  of Régie's Textual dependency (`PLC0415` disabled).
- Line length 100. Mypy checks annotated code; unannotated code is left alone.
- Existing large hosts (`RegieApp`, `ControlService`, `Observer`, `Store`) use
  concern mixins and a typing-only host (`_host.py` / `RegieHost`). Preserve that
  pattern when extending them; use explicit collaborators for independent lifecycles.
- **Régie imports only `theater.frontend`**, including in tests. Publish needed
  Theater values through the SDK; do not copy them or import internals.
- UI tests wait for conditions, not fixed sleeps
  (`packages/regie/tests/support/waiting.py`).
- Shared test helpers belong in `tests/support/`; import as
  `from tests.support.<module> import ...`. Test modules never import each other.
  Régie helpers belong in `packages/regie/tests/support/`.
- Keep the smallest test set that proves the behaviour. Remove overlapping
  coverage, but preserve distinct failure, rollback, cancellation, and recovery
  scenarios relevant to the change. The 80% coverage gate still applies.

## Invariants — do not break these

- **Keep core policy harness- and frontend-agnostic.** Use harness contracts,
  the public frontend API and declared provider capabilities. Keep harness-specific
  behavior in harness plugins and UI/terminal implementations in frontend/provider
  packages. Do not add core policy branches on harness/frontend/provider names or
  import their internals into core; verify the import and process-boundary tests.
- **The daemon is the sole writer** of orchestration SQLite state. It plans
  launches and authorizes controls; the selected terminal provider performs
  terminal creation, inspection, input, interruption, and termination through
  fenced callbacks. Régie's tmux bridge owns tmux execution, while its UI may
  change presentation only while preserving each terminal and occupant identity.
  MCP servers, frontends, and providers all write orchestration state through
  daemon RPCs; keep it that way.
- **`Participant.addressable` is physical, not a permission.** No verified
  terminal-provider or native-runtime route means no inbound control. Never
  treat `EXTERNAL` as merely "unprivileged".
- **Human presence is provider-supplied and fail-closed** (`daemon/presence/`;
  Régie's tmux implementation derives it from terminal focus).
  An input-capable attached client's terminal focus and selected input pane
  protect that participant; mouse position and Régie selection do not.
  Pane changes, terminal blur, and detach release protection. Copy mode
  (`pane_in_mode`) separately blocks unsafe legacy key injection, not safe
  native controls or presence-aware awaits. UNKNOWN protects like PRESENT:
  a missing or errored provider
  never manufactures absence, because a wrong "no human present" injects
  keystrokes into a pane a human is using, which is unrecoverable. Presence
  is reported asynchronously, so it lags reality: a stale absence is not a
  fresh one — consumers refresh at admission and wait on revisions, never on
  reported timestamps. Existing-participant mutations that touch a pane or its
  control flow require absence; registry metadata (name, description) is not gated —
  it has no pane channel, and neither does *admission* of a peer's plain `send` as a
  durable queued message: it touches no pane, and every promotion to delivery
  (`dispatch_queue`) re-gates fresh presence, so absence is never inferred from admission;
  `jobs.await` gates a target only on the presence observed at admission; a gated target waits for both an observed departure and a terminal job — a departure alone never releases a still-running job.
  Do not add screen-scraping heuristics here (one was removed for this).
- **`AWAITING_INPUT` is a display hint** — never gate a control decision on it.
- **A strict job's repair fence is its own fact.** `job_repairs.fence` is not
  `control_operations.execution_barrier`, and only positive evidence releases it:
  exact terminal evidence for the repair turn, a definitive rejection, or a gone
  native context. Never release it on idle, screen rescue, identity loss, or GC, and
  never repair a route that cannot name the answering turn — fail closed to
  `unable_to_parse` and audit it. Completion matchers use
  `completion_candidate_jobs_for_target`; safety checks keep
  `active_running_jobs_for_target`.
- **The three quiet timers stay separate** (`RELOCATE`, `AWAITING_INPUT`,
  `RESCUE` in `observation/reducer.py`). Sharing them was a v1 bug; the comments call it a
  scar, not a preference.
- **Approval has no default** anywhere — it is chosen per-spawn (`manual` /
  `edits` / `yolo`). This is the whole safety story for an unwatched child; do not
  add a global default.
- **Job states are `running`/`done`/`crashed`/`killed`.** `timeout` is not a state
  — it's what `await` returns when the caller stops waiting.
- **Observer job 1 (get the text) is the replaceable seam; job 2 (decide what it
  means) is not.** Per-harness behaviour belongs in a `Source` / `HarnessObserver`,
  never in the reducer's policy.
- **A handoff gate is never a pane write.** `after` on `spawn` reserves only rows
  (no worktree, argv or terminal) until the source job is terminal; on `send` it is
  an ordinary QUEUED control row whose readiness is checked in `_dispatch_head`
  *before* delivery, after which presence is gated fresh as for any queued message.
  A waiting head keeps its lane (nothing the same sender queued later overtakes it)
  and other senders' lanes go on. Source evidence (`job_evidence`: answer, provenance,
  pinned commit and dirty state) is written with the terminal state, from Git read
  *before* the write unit opens. Awaiting a gated handle also waits on the participant
  running its source (dependency-aware cycle check), and the await exception for work
  durably proven never dispatched never makes a participant `ABSENT`.
- **Schema edits go through Alembic.** A bare `CREATE TABLE IF NOT EXISTS` change
  is a silent no-op against existing databases (the v1.2 hazard). Regenerate a
  revision and run `alembic check`.
- **Rows are deletable, handles are not.** The send-sequence counter lives in the
  `meta` table because it used to be re-seeded from `MAX(jobs)`; once the GC can
  delete old jobs, that regresses the counter and re-mints handles pruned jobs
  already used. Anything else derived from `MAX(some table)` at start-up is the
  same bug waiting to happen — persist it.
- **The GC sweeps jobs on `finished_at`, never `created_at`,** so a running job
  (`finished_at IS NULL`) can never be deleted out from under a caller that is
  awaiting it. **And every sweep is batched** — the store is synchronous on the
  event loop, so one unbatched `DELETE` of 30k rows freezes every status poll
  and every await while it runs.
- **`VACUUM` is never background.** Only `theater gc --vacuum`, because it locks
  the whole file. Plain deletion does not shrink the file, and every path that
  reports a sweep has to say so — a user who deletes 94% of the database and
  sees the same file size reports GC as broken.
- **Observability has one process-level lifecycle.** `server.run()` owns daemon
  observability setup and shutdown; `cmd_daemon` owns only argument translation
  and exit codes. `runtime.configure()` is called exactly once — global tracer
  providers cannot be reset safely, so a second configure attempt is rejected.
  `var/logs/daemon/daemon.log` (rotating) and `var/logs/daemon/stderr/<token>.log`
  (raw crash output)
  are never the same file. Gauge sampling runs on the daemon event loop because
  Store's SQLite connection is loop-thread-only; exporter callbacks read only a
  cache and never query SQLite. See §13 of architecture.md.

## When adding a harness

Write a named package: `$THEATER_HOME/plugins/<name>/manifest.py` exporting
one immutable `MANIFEST`. The folder name is canonical; use relative sibling
modules and only public `theater.harness.contracts` APIs. Local packages
override shipped packages, disabled names are skipped before import, and a
legacy top-level `.py` is only a non-executing migration diagnostic. All
shipped harness-specific production code belongs in
`theater/harness/builtin/plugins/<harness>/`; `builtin/adapters/` and loose
`builtin/plugins/*.py` do not exist. There is no TOML shortcut — the deep half
of an adapter (turn boundaries, durable source semantics, native sub-agents)
cannot be expressed in config. Full guide:
[`docs/harness-plugins.md`](docs/harness-plugins.md). `theater harnesses`
reports what loaded and why anything was rejected.

## Changing a version gate or native wiring

Every shipped plugin reads its inclusive `min`, `max` and `excluded` from its own
`compatibility.json` through `theater.harness.contracts.version_policy.VersionPolicy`.
The minimum is the floor: the daily sweep raises it to bound the range at the newest
`HARNESS_COMPAT_MAX_RANGE` releases (repository variable, default 10) and it never falls;
supporting older releases is out of scope.
Treat the policy and the daily [compatibility pipeline](docs/harness-compat.md) as one contract:

- Keep policy files compact: one JSON line with a trailing newline. A routine gate PR replaces
  exactly that line in exactly one plugin; it adds no evidence, fixtures, docs or Python code.
  A raise leaves behind code only the old releases needed: delete it in a separate code PR from
  the ledger in `docs/harness-compat.md` ("After a floor raise"), never in the routine PR.
- The live manifest-derived conformance suite is the sole qualification authority. Changing
  runtime, launch, hooks or transcript behavior requires corresponding suite obligations.
- One qualification tests exactly its supplied version in a disposable code copy and home.
  No run may change the checkout or real home, save results, update fixtures or retain success
  history. Temporary CLI, daemon and test data must be deleted after the run.
- Only GitHub workflows invoke compatibility scripts. Daily runs test every stable release above
  committed max, in order; pending PR releases are rechecked without a success-history cache.
  Passes may advance max. Red, inconclusive and tooling outcomes get one reused harness/version
  issue; failures never move committed bounds. Publish each outcome before the next release.
  Create one per-harness PR only for a qualified ceiling increase, carrying required exclusions.
  Failure-only sweeps create no policy branch or PR; old exclusion-only branches are not revived.
  Failed pending ceilings are replaced by a freshly qualified extension or their PR is retired.
- Keep issue titles `Harness compatibility: <harness> <version>` and PR titles
  `chore(compat): <harness> <old max> → <new max>`. Link every excluded version to its issue and
  related issues back to the PR. Update only marked generated sections; preserve human notes.
  Retain links for exclusions pruned from earlier runs. Read issue history before policy
  publication; a failed lookup must never be treated as an empty history.
  A completed, reliably published sweep may succeed with red candidates; those remain unqualified.
  Incomplete, inconclusive/tooling and publication failures still fail the workflow.
- The workflow's `failures` job reads completed jobs' logs with GET requests only and lists their
  failure ledgers in its log and `$GITHUB_STEP_SUMMARY`, on PR runs too. It is the only writer of a
  step summary, never publishes or saves artifacts, and decides no verdict.
- PR checks qualify the proposed code again. A policy refusal never substitutes for a behavioral
  test: only the disposable copy may temporarily admit the supplied release.
- Routine PR verification tests its proposed extension and changed exclusions; unrelated new
  upstream releases cannot fail it. Recover known unfinished versions into their own issues;
  already published outcomes get no duplicate report. Unidentified, setup and reporter failures
  stay in Actions; never create generic workflow-error issues. PR runs remain read-only.
  Check the implementation with `uv run pytest tests/compat tests/conformance tests/harness/<h>`.

A plugin may also export an optional `compat=CompatManifest(provision, native_edit, mcp_call, mcp_discovery, canaries)` (see
`docs/harness-plugins.md`, "Qualification"). It is read only by the live conformance suite, never at
runtime; `provision` must stay endpoint, credential, catalog and onboarding setup. Scenarios are derived
from the rest of the manifest, so a new manifest capability needs a mapping in
`tests/support/coverage.py` (the completeness test fails until it has one) and, if it is provable,
a scenario family. Extra generic mutation choices belong in the plugin's `compat.canaries`, not
in shared harness-name branches. Canaries check the suite separately from release qualification.
Non-model mock routes belong in `compat.acknowledged_routes`; they never consume scripted replies.

## When adding a skill

Write one data-only package at `$THEATER_HOME/skills/<name>/SKILL.md`, or under
`theater/skills/builtin/` for a shipped skill. An MCP-server plugin may declare
names in its manifest and place packages under `skills/<name>/SKILL.md`. A skill
package may contain only `SKILL.md`; frontmatter contains exactly `name` and
`description`, with the canonical name matching the folder. Do not add scripts,
Python imports, references, templates, project scanning, or silent overrides.
The daemon owns discovery; MCP tools only forward `skills.list` and
`skills.load`. Users can refuse individual built-ins through the
`[skills] disabled` list in `config.toml` — a built-in-only denylist that
validates before filtering, so a broken bundled skill stays fatal; user and
plugin skills are unaffected.

A sender can also attach skills to another agent's task with `skills=[...]` on
`spawn_session`/`send`; the attached cards must fit `SKILL_ATTACH_MAX_BYTES`
(16 KiB combined).

A **play** is `skills/<play>/PLAY.toml` (data only: description, master,
`[positions]`, `[roles]`) plus exactly one `<member>/SKILL.md` per declared
position and role, referenced as `<play>/<member>`. Plays may be user, built-in,
or MCP-plugin packages. Plugin manifests declare `plays` and supported `play_permissions`;
`PLAY.toml` may declare `plugins` and per-position `[plugin_permissions.<plugin>]`
grants. The daemon checks dependencies and attachments; the plugin enforces its
tool policy using authenticated play context. Beads ships the default hierarchy
play under `mcp_plugins/builtin/beads/skills/`. Membership lives in `daemon/plays/`: loading the master
skill starts a play with pinned rules, and every spawn below a live master is
checked inside the spawn's accepting transaction — keep new spawn entry points
on `ParticipantLaunchService` so none bypass it. A queued send carrying play
skills is rechecked in the dispatcher's `_check_gate` and fails unsent once its
binding no longer holds; sends without play skills never touch that gate.

## Further reading

- [`docs/architecture.md`](docs/architecture.md) — why every piece is shaped this way (the authoritative doc)
- [`docs/harness-plugins.md`](docs/harness-plugins.md) — writing an adapter
- [`docs/harness-compat.md`](docs/harness-compat.md) — the daily pipeline that qualifies new upstream harness CLI releases (`scripts/harness_compat/`)
- [`docs/mcp-server-plugins.md`](docs/mcp-server-plugins.md) — MCP sidecars, skills, and plays
- [`config.example.toml`](config.example.toml) — every setting at its default
