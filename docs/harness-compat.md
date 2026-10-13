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

The command exits `0` when every conclusive result is green (including no new release), `1` for
a red compatibility result, and `2` for inconclusive or tooling failure. Evidence JSON is the
stable `Result.to_evidence()` object: harness, version, verdict, adapter digest, then ordered
checks with name, status, and detail.

Publishing calls `build_report(harness, results, run_url=..., routes=...,
admitted_versions=...)` from `scripts.harness_compat.report`, then
`write_github_outputs(report, Path(os.environ["GITHUB_OUTPUT"]))`. The workflow consumes
`pr_needed`, `pr_title`, `pr_body`, `issue_action`, `issue_title`, `issue_body`, and
`issue_labels`.
