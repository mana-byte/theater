# Harness compatibility

Each plugin owns one compact `compatibility.json`, read by runtime admission and scripts:

```json
{"min":"0.163.0","max":"0.164.0","excluded":[]}
```

Bounds are inclusive. Min is the floor: the sweep raises it to keep at most the newest N releases
in range (N is the `HARNESS_COMPAT_MAX_RANGE` repository variable, default 10), and it never falls.
Exclusions may exceed max inside a ceiling-raising PR. A failed newer release gets a version issue;
an all-failure sweep creates no policy branch or PR. Invalid policies and unsupported binaries fail
closed. OpenCode has one plugin/channel (`@opencode/cli`); 1.x is removed.

## Ownership and flow

Only GitHub workflows invoke `scripts/harness_compat`; there are no local qualification targets.

```text
workflow → scripts → exact harness/version → generic live suite
                   → passing release: shared ceiling-raising PR
                   → red/inconclusive/tooling: harness/version issue
                   → ceiling PR: retain failed-version exclusions and raise min when needed
```

The daily workflow discovers every stable release **above committed max**, tests them in ascending
order and continues after failures. It does not start above the pending PR's max: pending releases
are rechecked on subsequent runs, without a passing-version cache.

Scripts publish completed outcomes before testing the next release. Updates include earlier
outcomes from the same run, so a temporary publication failure cannot let a later pass admit a
known failed version. Discovery failure stays in Actions and authorizes no untested release.
An outcome whose publication fails is retained as a tooling failure (or an interrupted run),
so the next prefix proposes its exclusion before authorizing later releases.

Passing versions may advance max; **failures never move committed bounds**. Every red, inconclusive
or tooling result gets one issue per harness/version, reused across runs and reopened after a new
failure if closed. No generic workflow-error issues are created. Issues from earlier failure-only
sweeps fence later ceiling proposals until that exact version passes fresh qualification.
Issue history must be read successfully before publication; a lookup failure blocks all remote
writes rather than treating the history as empty.
Required exclusions travel with a useful ceiling increase; failures alone never produce a PR.
Issues distinguish failed contract assertions from infrastructure, timeout and unexpected exceptions;
none of those outcomes alone establishes an upstream binary defect.

Issue titles stay `Harness compatibility: <harness> <version>`. PR titles show the ceiling change:
`chore(compat): <harness> <old max> → <new max>`. Each excluded version is listed with its issue link
in the PR body, and related issues link back to the PR. An issue stands alone until a useful PR
exists. Automation replaces only its marked report/link sections; human diagnoses, decisions and
links survive reruns. Existing unmarked descriptions are preserved when those sections are added.
Both links are retained when a floor increase prunes exclusions recorded in earlier runs.
Issue reports retain up to twelve failure excerpts of 1,000 characters each; the workflow's compact
table supplements the individual check details printed in its logs.
Every `run` and `verify` ends with a ledger between `compat:failures:begin` and `compat:failures:end`:
each non-green version with all its failing checks, run errors and unfinished releases. A failing
scenario's detail leads with the last warnings of the daemon, bridge and raw stderr logs its stack
captured, which would otherwise vanish with the sandbox; no log or result is retained.

A completed daily sweep succeeds when testing and publication finish reliably, even when candidates
are red. Their version verdicts remain red and those versions stay unqualified. Incomplete runs,
inconclusive/tooling outcomes and publication failures fail the workflow. PR admission checks stay
strict; a successful sweep never establishes compatibility for a failed candidate.

When the discovered range holds more than N releases, publication also raises min to the oldest
release of the newest N (never an excluded one); exclusions below the new floor are pruned, the
floor never falls, and releases below it are never retested, so later PR windows only shrink.
Moving N itself re-qualifies and re-verifies nothing: the floor is recomputed from the discovery at
the next publication. Routine PR checks recompute the floor from a fresh discovery and require the
PR's min to match it exactly; a deliberate larger raise goes through a reviewed code PR.

Scripts create or update `chore/compat-<harness>` only for an admitted ceiling above committed max,
preserving pending exclusions. Old exclusion-only branches cannot generate replacement PRs.
If a pending ceiling fails requalification, its unmerged increase is withdrawn: a lower freshly
qualified extension can replace it, otherwise the PR is closed without a policy-branch write.
An exclusion-only or otherwise obsolete open automation PR is also closed; its branch is retained.
A routine PR changes **exactly one file, +1/−1**: the plugin's JSON line. It contains no fixture,
history, evidence, Python or documentation edits. Publication uses GitHub APIs without editing the
qualification checkout. Concurrent branch updates are refused rather than overwritten.
A moving main rebases the proposed JSON; fresh PR checks prove admissions on the proposed code.

Routine PR checks cover the proposed extension and every changed exclusion, including removals;
unrelated newer upstream releases cannot make them fail. Wiring/code PRs recheck the current policy
window: a per-harness fan-out splits the window into shards of about four releases (at most eight
per harness) that verify in parallel, each shard receiving its explicit release list, and a shard
list outside the window is refused. PR verification shares the sweep runner's guards, including
the tooling short-circuit: three identical tooling errors in a row record the rest of a shard
inconclusive, which fails the check. Excluded contract failures do not admit that release;
inconclusive checks still fail CI.
Every `pull_request` run is read-only, including its `workflow_run` reporter: failures remain that
PR's failed checks. Setup failures, timeouts, cancellations and reporting/API errors never create,
edit or comment on issues, policy branches or PRs for a PR-origin run.

A separate `workflow_run` reporter uses trusted main code and system Python, independent of uv
setup. Only scheduled/manual release sweeps on main can publish: the workflow job guard and Python
reporter both enforce this before job-log recovery or publication. It recovers unfinished known
harness/version outcomes from trusted qualification logs and reuses their version issues; already
published outcomes are not reported again. It never authorizes a pass from logs. Discovery,
setup/static-check, job-log/API and unidentified failures remain in Actions with the failing job,
step and available error details. Reporter exceptions also stay visible failures, with no generic
issue fallback. Foreign/unidentified source logs never supply version identities. Canary runs are not
reported: a red one says the suite missed a planted break, not that a release is bad, so it stays
a red check or run instead of an issue and an exclusion.

## Suite and temporary data

`tests/conformance/live/` is the sole qualification authority. Its scenarios derive from the
selected plugin's production manifest and exercise a real daemon, Régie provider, private tmux
server and stock CLI through public contracts against a scripted local model.

Harness-specific onboarding, endpoints, dummy credentials and tool syntax live in the plugin's
`CompatManifest`. Canary declarations also live there; shared scenarios, assertions and mutations
are generic. Canaries check suite sensitivity separately, weekly and when wiring changes.

Each qualification installs and verifies **exactly its supplied version**, then runs in a disposable
source copy and home. Only that copy may temporarily admit the candidate. Required/inventory
reports, CLI/daemon files and mock traffic are temporary and deleted after the run. No source
checkout or real home writes, saved results, fixture updates, artifacts or success history.

Only a complete passing suite can advance max. Missing scenarios, skips, invalid inventories,
timeouts, non-clean subprocess exits and cleanup failures cannot authorize a pass.
Cleanup waits for owned processes before deleting data; already-missing child files are harmless.
A cleanup failure retains completed checks and the actual error, stops the sweep, and cannot pass.
Distinct parser, safety/recovery and SDK regressions remain supplementary.

## Remaining suite limits

Available OTel channels have no live payload scenario and make qualification inconclusive.
Native child stimulation, timing/usage accounting and foreground MCP timeouts retain explicit
exemptions in `tests/support/coverage.py`; ordinary regressions cover their current behavior.

Hosted sweeps interrupt after 320 minutes, leaving five minutes for cleanup and publication.
The sweep also carries a 310-minute time budget: a release that cannot start with at least ten
minutes of sweep time left is recorded inconclusive and published instead of left untested, and
the rest of the sweep is skipped the same way. Three identical tooling errors in a row record
the remaining releases the same way. Skipped releases stay above max and are retested by the
next sweep. Finished outcomes are retained in the shared
PR/issues; an unfinished known version gets its version issue and remains unqualified.
OpenCode's 2.0.18–2.0.20 renderer recovery remains independent of policy because baseline 2.0.18
needs it.
