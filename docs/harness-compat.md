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

To add a harness, implement the frozen `Adapter` in
`scripts/harness_compat/adapters/<name>.py`, export `ADAPTER`, and register its name in
`adapters/__init__.py`. Add its real-binary tests, expected model routes, and gate files,
then add it to the workflow matrix. Keep installs and CLI invocations inside `make_sandbox`.
