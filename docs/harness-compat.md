# Harness compatibility

The daily workflow qualifies stable releases of Claude Code, Codex, opencode (1.x and 2.x), Pi, and Vibe
without real API keys or user configuration. It updates one rolling gate PR per harness and
one issue per harness for red, tooling, or still-inconclusive results. An inconclusive release
never justifies a gate bump; it is retried (below) and, if it stays inconclusive, becomes an
`inconclusive`-labelled issue.

Run every harness locally:

```sh
make compat
```

Select one harness or release with `make compat HARNESS=codex` or
`make compat HARNESS=codex VERSION=0.162.1`. Local runs are dry runs. A maintainer can use the
workflow dispatch form to publish. CI alone passes `--publish`; PR creation uses
`PR_AUTOMATION_TOKEN`, while issue publication uses the workflow token with `issues: write`.

The command exits `0` for green or no work, `1` for a red or tooling row, and `2` for usage
errors or when only inconclusive rows remain after the retries. `--summary-json PATH` writes
the detailed results. A local run never edits the repo or publishes anything, but it caches
green results outside the checkout.

Green evidence lives at `$HARNESS_COMPAT_CACHE/<harness>/<version>.json`, or under
`$XDG_CACHE_HOME/theater/harness-compat` (default `~/.cache/theater/harness-compat`).
GitHub Actions restores and saves that directory between runs. The Codex bundle a green run
captured lives beside the cached JSON, so a reused green can still refresh the retained
`latest` fixture. The rolling PR changes the plugin gate (for Codex one ceiling line) and, only
when the captured evidence differs, that one fixture; its body has one verdict and run link per
release.
The detailed checks stay in the workflow run. A red issue carries `compat:<harness>`;
`tooling` marks an automation or mock failure, `admitted-regression` marks an in-range
release that failed the daily watch, and `inconclusive` marks a release whose three attempts
all ended in infrastructure noise. The issue closes once every release it lists is proved
again or retired (see the ledger below).
Issue creation, updates, labels, and closure use the GitHub REST API; an issue is found by its
`compat:<harness>` label and exact title.

## How releases are chosen

- A candidate is a stable upstream release without reusable cached evidence and outside the
  harness's `known_bad` list. `--version X` forces one release.
- Range gates (Claude, Codex, OpenCode 1.x and 2.x, Pi, Vibe): the oldest releases at or above the current
  ceiling, tested in ascending order. `--max-candidates N` (default 5) caps the run. The first
  red or inconclusive result stops the run (the untested rest is reported as deferred), and the
  bump goes only to the last contiguous green. A range gate also qualifies its
  newest admitted release every day: a red result there opens an `admitted-regression` issue,
  while a green one stays silent.
  A forced `--version` is promoted only when every discovered release below it is proved too.
- Codex's range is a record of run releases, not a semver promise (evidence outranks semver):
  the ceiling moves only over an unbroken run of greens, a release that cannot be proved goes
  into `CODEX_RUNTIME_QUALIFIED_EXCLUDED` with its measured cause, and anything outside the
  range stays on the legacy launch. The retained `latest` fixture is rewritten only when a
  capture's evidence differs, so unchanged evidence bumps one ceiling line.
- Claude's gate is one inclusive literal, `_QUALIFIED_MAX` in
  `theater/harness/builtin/plugins/claude/compatibility.py`, next to the hook floor. It covers
  the asynchronous hook surface only: the messaging range stays display-only (`runtime=None`) and
  is not qualified by this pipeline. A release newer than the maximum has its optional hooks
  omitted until the gate PR is merged; releases between the floor and the maximum are admitted
  without a proof of their own.
- OpenCode is qualified as two harnesses because its two lines ship as different npm packages
  and keep separate gates: `opencode` (1.x, package `opencode-ai`, `OPENCODE_TUI_MAX_VERSION`) and
  `opencode2` (2.x, package `@opencode/cli`, `OPENCODE_SERVER_V2_MAX_VERSION`, whose exclusive
  ceiling may end in `.0`). Each has its own cache, rolling PR and issue. The 2.x proofs
  (`tests/integration/opencode/test_server_v2_proof.py`) are model-free: banner, Basic auth,
  `/api/info`, session location and idle absence, a durable prompt admission read back through
  the lineage database, and the production-rendered approval plugin proven active with the
  runtime's own fail-closed check. A 2.x model turn and the TUI attach are not proven, and the
  exact release list in `tui_recovery_v2.py` stays human-owned.
- Vibe's floor is enforced at launch as well as displayed: a release older than
  `_FLOOR` ignores the isolated session-log directory, so launch planning refuses it.
- An adapter that changes no gate value produces no PR.
- Bump adapters share AST literal reading and checked line replacement through
  `scripts/harness_compat/gate_literals.py`. Each adapter keeps its own accepted
  syntax, duplicate handling, diagnostics and release-bound policy.
- Cached evidence covers the import closure seeded by framework and adapter
  sources, proof tests with their helpers and conftests, and the declared plugin
  and data inputs. Changes require qualification again.

## Retries and outcomes

A release is qualified in a fresh worktree, home and mock; so is the composed bump. Only an
`inconclusive` result (install, timeout or tooling noise that proves nothing about upstream)
is repeated, at most
three attempts in total with 30 s and 90 s backoff; red and tooling are never retried, but a
timed-out proof run counts as noise whatever an adapter called it. A run has a 70-minute budget
plus a 20-minute reserve for one attempt, which is the 90-minute job timeout: a retry or a
further candidate starts only if the reserve still fits, otherwise it is shown as not run or
deferred. The first attempt of the watch and of the composition always runs, so the job timeout
is the hard bound. Each row (candidate, watch, composition) records its attempts.

| Rows after the retries | PR | Issue | Exit |
| --- | --- | --- | --- |
| contiguous green prefix, composition passes | yes, for that prefix | only for other failing rows | per rows below |
| any red or tooling row | independent of this | opened or updated | 1 |
| only inconclusive rows | none for them | opened or updated, `inconclusive` | 2 |
| all green or no work, every listed release proved again or retired | per prefix | closed | 0 |

Any final non-green result (red, tooling, or exhausted inconclusive) revokes that release's
cached green. An open gate PR whose body lists a release that ended non-green in this run is
closed after the PR step, so a recomputed replacement survives; a PR that lists none of them is
left alone. The issue body ends with a `compat-failing` marker: the issue closes only once every
release it lists was proved again or retired (below the floor, or `known_bad`); a release this
run did not retest stays listed as still unresolved, the list shrinks as releases recover, and an
entry that is not a release (a hand-edited marker) keeps the issue open. Only files under an adapter's declared
`artifact_roots` (Codex: its candidate bundle directory) are captured and can enter a PR;
everything else a qualification leaves in the tree is dropped. Evidence is reused only for the
same digest: framework and adapter source modules seed the import closure alongside
proofs and their imports. Explicit plugin/data inputs, `uv.lock` and `pyproject.toml`
remain included. New shared helpers therefore invalidate the affected evidence.
A digest change invalidates reusable evidence, so those proofs run again; an open
gate PR is touched only when a release it lists ends non-green.

## Reading a run

- The run name says what was selected (`claude @ 2.1.305 (workflow_dispatch)`); on a PR it says
  `static checks`, because a PR only runs the lint, type and unit checks, never an upstream
  release.
- Dispatching one harness creates only that harness's job. A version needs exactly one harness.
- Each job writes a summary (gate, candidates, deferred, every row with its attempts and
  cause) and annotates each failing row; the `Result overview` job adds one table over all
  selected harnesses and fails when one of them left no result, has a red, tooling or
  inconclusive row, or when a qualification job (including its publishing steps) failed.
- The summary JSON, the PR and issue bodies and the diagnostics are uploaded as
  `compat-result-<harness>` for 14 days.
- PRs and issues are produced only for runs on `main`; any other ref is a dry run of its own
  code (the workflow checks out the triggering commit).

## Reading a PR or an issue

- Declared skips appear once in a PR. OpenCode's two stock-TUI proofs and the
  Régie dashboard assertion are not run by the pipeline, so its TUI gate bump rests on the
  server proofs and the probe alone: review that part by hand.
- "Historical references to the previous limit remain" lists up to five code mentions of an old
  limit. They are usually harmless history; an old limit still written as an active
  bound is a `tooling` failure instead.
- Route counts show which mock model routes the CLI called. An unexpected route on a run that
  is not green makes the result `tooling`, never an upstream red.
- Failed rows carry the first failing test id and the cause of the failure, trimmed.

## Native qualification evidence

Codex qualification runs the unmodified stock binary against a
[scripted local model provider](../tests/native/codex_mock_provider.py). It exercises
turn identity, steer and interrupt, concurrent submissions, approval ownership,
settings and queue gates, and stock-TUI attachment. Held streams keep active-turn
checks deterministic; matching the last user message avoids replaying earlier prompts.

The [capture tool](../tests/native/codex_qualify_runtime.py) records schema and behavior
evidence, marking mock captures with `"provider": "mock"` in `installed_release.json`.
These proofs establish protocol compatibility, not real-model refusal behavior,
multi-tool reasoning, or realistic latency. Use `make compat HARNESS=codex VERSION=X.Y.Z`
to qualify a candidate before changing its gate; refresh the retained fixture only when
the evidence changes.

## Live conformance suite (shadow period)

One harness-agnostic suite, `tests/conformance/live`, exercises every Theater capability a plugin
declares against a candidate release: a real `theater daemon`, the real Régie bridge and the stock CLI
run in a throwaway sandbox against a mock model (Anthropic Messages, OpenAI Chat Completions,
OpenAI Responses) and are driven only through `theater.frontend`. A plugin declares almost nothing: the
optional `compat` manifest (`provision`, `native_edit`, `mcp_call`, see `harness-plugins.md`) points the CLI
at the mock; every scenario is derived from the rest of the manifest (`tests/support/obligations.py`).

```sh
make suite HARNESS=vibe                    # the installed CLI, every derived scenario
make suite HARNESS=vibe ARGS="-- -k mcp"   # pytest arguments go after a lone --
make canaries HARNESS=vibe                 # the suite must notice deliberately broken plugins
```

Each obligation is `family:detail`: `launch:<approval>`, `turn:prompt`, `interrupt`, `steer` (or
`steer:refused` where the runtime declares steering unavailable), `resume` (history reaches the model
once and the daemon records the lineage), `model` and `effort` (two selections reach the wire as two
values, a repeat is stable), `mcp:gated|call|refusal` (the approval policy gates or runs the probe
sidecar's tool; a refusal from Theater's own MCP server reaches the model), `edit:gated|runs` (the
CLI's own file-write tool is gated under `manual` and runs under `edits`) and
`runtime:native|legacy|survival|unavailable-<capability>` (a native runtime is a connected route with a
session identity; under a manifest variant whose probe refuses the release, which is what an unqualified
release gets in production, the pane path still carries a turn; after a graceful daemon restart the same
backend and UI processes are still alive and the next turn continues the same conversation; a capability
the runtime declares unavailable is refused) and `channels:<id>` for each available hook channel (the stock CLI delivers an accepted event on
the declared channel during a tool turn, and a source other than the transcript contributes a trajectory
fact carrying the same tool-call id the mock issued; an available OTel channel ends inconclusive until a
scenario exists, and an unavailable channel has no obligation: the day it is declared available it is
derived and proven). Every manifest declaration is mapped to a family or a reasoned exemption in
`tests/support/coverage.py`, and `tests/conformance/test_completeness.py` fails when one is not.

Oracles do not rely on the plugin's own parsers except where the plugin's screen classifier is itself the
subject: filesystem effects, the mock's request records (model, effort, offered tools and their declared
arguments, human messages, tool results), job results and the daemon's route and identity facts. An oracle
violation, or a proven contract violation by the CLI, fails (red, never cancelled by a later pass); an
environmental or unattributed failure skips as `inconclusive` and the runner retries it. A CLI exit is
blamed on the CLI only while the daemon, its connection, the mock, the private tmux server and an online
terminal provider are all proven alive. A scripted tool call that does not fit the schema the CLI offered
(an unknown argument on a closed schema, a missing required one) means the plugin's `native_edit` or
`mcp_call` recipe is stale for that release: nothing is proven about the CLI, so the run is inconclusive,
never red. The suite writes
`required.json` before it runs and `inventory.json` after; `scripts/harness_compat/suite_runner.py`
reconciles them, and an obligation whose scenario never ran is a `tooling` failure rather than a green.

Shadow mode: an adapter with `shadow_suite = True` runs the suite after a conclusive verdict. The
per-obligation checks ride on `Result.shadow_checks`, appear as `shadow` in the evidence document and as a
table in the job summary, and never change the verdict; a failing run's logs, pane captures and request
records go to the diagnostics artifact. A harness switches authority only after at least 14 days and 30
paired old/new runs over three consecutive releases with no unexplained disagreement, every applicable canary
detected, a median attempt of at most 6 minutes (p95 8, hard cap 20) and at most 5% first-attempt
infrastructure noise. After the switch the adapter's `green_tests` is `tests/conformance/live`, the
evidence digest covers the daemon and bridge closures the suite starts (Régie's UI excluded), and obligations
that vanish at an unchanged plugin turn a green red (`inventory`, keyed by a plugin digest with the verified
gate literals normalised so an ordinary bump keeps the baseline).

Isolation: the suite is collected only under the pipeline sandbox token, so `make test` never starts it.
Every path it touches lives under the sandbox root, the environment of the daemon, bridge and CLI is built from
scratch (dead proxy, private tmux, throwaway home), the version probe runs from a throwaway home, and the
guards are unit-tested (`tests/conformance/test_guards.py`). The weekly `Harness compatibility canaries`
workflow installs each harness's newest admitted release and requires the suite to fail the obligations that
a manifest-level mutation breaks (manual mapped to yolo, edits to manual, model or resume dropped, inert
interrupt keys, launch env or files dropped, a native runtime the probe refuses); it also runs when the
suite changes.

## Requirements and limits

- A run needs network (npm, PyPI, GitHub release downloads), `tmux`, Node.js 22 with npm, and `uv`.
  Discovery calls GitHub's API without a token, which is rate limited; Codex falls back to public
  Git tags. No model API key is ever needed: every CLI talks to a local mock with a dummy
  credential inside a throwaway home.
- Every run kills the processes and tmux servers it started from its sandbox, detached ones
  included, on success, failure and timeout. Ownership is an environment token, so a process
  that clears its environment escapes the sweep (tmux servers are stopped by socket instead).
- `uv run python -m scripts.harness_compat check` only confirms that every adapter reads its gate.
- The live suite proves OpenCode 2.x only. Its 1.x line needs an online prewarm of the first plugin
  load (any `plugin` entry makes `POST /session` hang offline, which the old adapter's
  `prewarm_catalog` supplied) and a `filePath` recipe for its write tool (2.x's closed schema names the
  target `path`, and one static `native_edit` cannot carry both). Until the suite grows a prewarm hook
  and per-release recipes, the 1.x gate stays with the adapter's own proofs.

To add a harness, first give its plugin a `compat.py` (see `harness-plugins.md`, "Qualification") so the live
suite can run it, then implement the frozen `Adapter` in
`scripts/harness_compat/adapters/<name>.py`, export `ADAPTER`, and register its name in
`adapters/__init__.py`. Add its real-binary tests, expected model routes, and gate files,
then add it to the `plan` job's list and the dispatch options, and to the canaries workflow's matrix. An adapter may set `shadow_suite = True` and list the literals its bump rewrites in `gate_literals`. Keep installs and CLI invocations inside `make_sandbox`. Pi's install pins its `@earendil-works/*` dependencies to the range floor upstream shipped, since npm would otherwise resolve carets to a newer, incompatible sibling.
