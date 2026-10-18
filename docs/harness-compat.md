# Harness compatibility

The daily workflow qualifies stable releases of Claude Code, Codex, opencode, Pi, and Vibe
without real API keys or user configuration. It updates one rolling gate PR per harness and
one issue for red or tooling results; inconclusive runs publish neither.

Run every harness locally:

```sh
make compat
```

Select one harness or release with `make compat HARNESS=codex` or
`make compat HARNESS=codex VERSION=0.162.1`. Local runs are dry runs. A maintainer can use the
workflow dispatch form to publish. CI alone passes `--publish`; PR creation uses
`PR_AUTOMATION_TOKEN`, while issue publication uses the workflow token with `issues: write`.

The command exits `0` for green or no work, `1` for red or tooling, and `2` for usage or
infrastructure errors, including an inconclusive qualification. `--summary-json PATH` writes
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
`tooling` marks an automation or mock failure, and `admitted-regression` marks an in-range
release that failed the daily watch. The issue closes after a conclusive green run.
Issue creation, updates, labels, and closure use the GitHub REST API. Claude's green results
remain cache-only and never produce a PR.

## How releases are chosen

- A candidate is a stable upstream release without reusable cached evidence and outside the
  harness's `known_bad` list. `--version X` forces one release.
- Range gates (Claude, Codex, OpenCode, Pi, Vibe): the oldest releases at or above the current
  ceiling, tested in ascending order. `--max-candidates N` (default 5) caps the run. The first red stops
  the run, and the bump goes only to the last contiguous green. A range gate also qualifies its
  newest admitted release every day: a red result there opens an `admitted-regression` issue,
  while a green one stays silent.
  A forced `--version` is promoted only when every discovered release below it is proved too.
- Codex's range is a record of run releases, not a semver promise (evidence outranks semver):
  the ceiling moves only over an unbroken run of greens, a release that cannot be proved goes
  into `CODEX_RUNTIME_QUALIFIED_EXCLUDED` with its measured cause, and anything outside the
  range stays on the legacy launch. The retained `latest` fixture is rewritten only when a
  capture's evidence differs, so an unchanged schema bumps one ceiling line.
- An adapter that changes no gate value produces no PR, including Claude.
- Cached evidence is reused only while the framework, the adapter, the proof tests
  with their imported helpers and conftests, and the harness plugin are unchanged; otherwise the
  release is qualified again.

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
then add it to the workflow matrix. Keep installs and CLI invocations inside `make_sandbox`. Pi's install pins its `@earendil-works/*` dependencies to the range floor upstream shipped, since npm would otherwise resolve carets to a newer, incompatible sibling.
