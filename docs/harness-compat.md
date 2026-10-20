# Harness compatibility

The daily workflow qualifies stable releases of Claude Code, Codex, opencode, Pi, and Vibe
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
all ended in infrastructure noise. The issue closes after a conclusive green run.
Issue creation, updates, labels, and closure use the GitHub REST API; an issue is found by its
`compat:<harness>` label and exact title.

## How releases are chosen

- A candidate is a stable upstream release without reusable cached evidence and outside the
  harness's `known_bad` list. `--version X` forces one release.
- Range gates (Claude, Codex, OpenCode, Pi, Vibe): the oldest releases at or above the current
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
  capture's evidence differs, so an unchanged schema bumps one ceiling line.
- Claude's gate is one inclusive literal, `_QUALIFIED_MAX` in
  `theater/harness/builtin/plugins/claude/compatibility.py`, next to the hook floor. It covers
  the asynchronous hook surface only: the messaging range stays display-only (`runtime=None`) and
  is not qualified by this pipeline. A release newer than the maximum has its optional hooks
  omitted until the gate PR is merged; releases between the floor and the maximum are admitted
  without a proof of their own.
- An adapter that changes no gate value produces no PR.
- Cached evidence is reused only while the framework, the adapter, the proof tests
  with their imported helpers and conftests, and the harness plugin are unchanged; otherwise the
  release is qualified again.

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
| all green or no work | per prefix | closed | 0 |

Any final non-green result (red, tooling, or exhausted inconclusive) revokes that release's
cached green. If that leaves no promotable release, the run closes the open gate PR, but only
when the PR body lists a revoked release. Only files under an adapter's declared
`artifact_roots` (Codex: its candidate bundle directory) are captured and can enter a PR;
everything else a qualification leaves in the tree is dropped. Evidence is reused only for the
same digest: the framework, adapter, proofs and every module they import from `theater`, the
plugin, `uv.lock` and `pyproject.toml`. A digest change empties the cache, so a gate PR from
an older digest stays open until it is merged or recomputed.

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

## Requirements and limits

- A run needs network (npm, PyPI, GitHub release downloads), `tmux`, Node.js 22 with npm, and `uv`.
  Discovery calls GitHub's API without a token, which is rate limited; Codex falls back to public
  Git tags. No model API key is ever needed: every CLI talks to a local mock with a dummy
  credential inside a throwaway home.
- Every run kills the processes and tmux servers it started from its sandbox, detached ones
  included, on success, failure and timeout. Ownership is an environment token, so a process
  that clears its environment escapes the sweep (tmux servers are stopped by socket instead).
- `uv run python -m scripts.harness_compat check` only confirms that every adapter reads its gate.

To add a harness, implement the frozen `Adapter` in
`scripts/harness_compat/adapters/<name>.py`, export `ADAPTER`, and register its name in
`adapters/__init__.py`. Add its real-binary tests, expected model routes, and gate files,
then add it to the `plan` job's list and the dispatch options. Keep installs and CLI invocations inside `make_sandbox`. Pi's install pins its `@earendil-works/*` dependencies to the range floor upstream shipped, since npm would otherwise resolve carets to a newer, incompatible sibling.
