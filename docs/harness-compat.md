# Harness compatibility

The daily workflow qualifies stable releases of Claude Code, Codex, opencode, Pi, and Vibe
without real API keys or user configuration. It updates one rolling evidence PR per harness and
one issue for red or tooling results; inconclusive runs publish neither.

Run every harness locally:

```sh
make compat
```

Select one harness or release with `make compat HARNESS=codex` or
`make compat HARNESS=codex VERSION=0.162.1`. Local runs are dry runs. A maintainer can use the
workflow dispatch form to publish. CI alone passes `--publish`; its publishing steps authenticate
only with `PR_AUTOMATION_TOKEN`.

The command exits `0` for green or no work, `1` for red or tooling, and `2` for usage or
infrastructure errors, including an inconclusive qualification. `--summary-json PATH` writes
the detailed results. A local run never edits the repo or publishes anything.

Each green release adds one JSON file under `tests/fixtures/harness_compat/<harness>/`.
The rolling PR shows release verdicts, checks, mock route counts, and the workflow run.
Review its code bump and evidence before merging. A red issue carries `compat:<harness>`;
`tooling` marks an automation or mock failure, and `admitted-regression` marks an in-range
release that failed the daily watch. The issue closes after a conclusive green run.
Some releases produce an evidence-only PR: the probe ceiling stays unchanged, and the PR body
says so. A human decides whether to raise that ceiling after reviewing the proof.

## How releases are chosen

- A candidate is a stable upstream release with no evidence file that is not on the harness's
  `known_bad` list. `--max-candidates N` (default 5) caps a run; `--version X` forces one release.
- Exact gates (Codex): the newest unadmitted releases that are newer than the oldest admitted one.
- Range gates (Claude, OpenCode, Pi, Vibe): the oldest releases at or above the current ceiling,
  tested in ascending order. The first red stops the run, and the bump goes only to the last
  contiguous green. A range gate also qualifies its newest admitted release every day: a red
  result there opens an `admitted-regression` issue, a green one stays silent.
  A forced `--version` is promoted only when every discovered release below it is proved too.
- Claude's gate has no ceiling to move, so its green releases produce evidence only.
- Evidence on the rolling branch is reused only while the framework, the adapter, the proof tests
  with their imported helpers and conftests, and the harness plugin are unchanged; otherwise the
  release is qualified again.

## Reading a PR or an issue

- `skip` rows are skips the adapter declared up front. OpenCode's two stock-TUI proofs and the
  Régie dashboard assertion are not run by the pipeline, so its TUI gate bump rests on the
  server proofs and the probe alone: review that part by hand.
- "Historical references to the previous limit remain" lists mentions of the old limit that the
  bump left alone. They are usually harmless history; an old limit still written as an active
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
  included, on success, failure and timeout.
- `uv run python -m scripts.harness_compat check` only confirms that every adapter reads its gate.

To add a harness, implement the frozen `Adapter` in
`scripts/harness_compat/adapters/<name>.py`, export `ADAPTER`, and register its name in
`adapters/__init__.py`. Add its real-binary tests, expected model routes, and gate files,
then add it to the workflow matrix. Keep installs and CLI invocations inside `make_sandbox`.
