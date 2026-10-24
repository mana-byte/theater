# Harness compatibility

Each plugin owns one compact `compatibility.json`, read by runtime admission and scripts:

```json
{"min":"0.163.0","max":"0.164.0","excluded":[]}
```

Bounds are inclusive. Min is pinned to the installed CLI baseline. Exclusions may exceed max:
a failed newer release needs an exclusion PR even when no release passes. Invalid policies and
unsupported binaries fail closed. OpenCode has one plugin/channel (`@opencode/cli`); 1.x is removed.

## Ownership and flow

Only GitHub workflows invoke `scripts/harness_compat`; there are no local qualification targets.

```text
workflow → scripts → exact harness/version → generic live suite
                   → passing release: advance max
                   → red/inconclusive/tooling: issue + exclusion
                   → one shared policy PR per harness
```

The daily workflow discovers every stable release **above committed max**, tests them in ascending
order and continues after failures. It does not start above the pending PR's max: pending releases
are rechecked on subsequent runs, without a passing-version cache.

Scripts publish completed outcomes before testing the next release. Updates include earlier
outcomes from the same run, so a temporary publication failure cannot let a later pass admit a
known failed version. Discovery failure reports a workflow issue and authorizes no untested release.
An outcome whose publication fails is retained as a tooling failure (or an interrupted run),
so the next prefix proposes its exclusion before authorizing later releases.

Passing versions may advance max; **failures never move either bound**. Every red, inconclusive or
tooling result gets an issue and an exclusion, including failures above max and all-failure batches.
Issues distinguish failed contract assertions from infrastructure, timeout and unexpected exceptions;
none of those outcomes alone establishes an upstream binary defect.

Scripts create or update `chore/compat-<harness>`, preserving pending range/exclusion changes.
A routine PR changes **exactly one file, +1/−1**: the plugin's JSON line. It contains no fixture,
history, evidence, Python or documentation edits. Publication uses GitHub APIs without editing the
qualification checkout. Concurrent branch updates are refused rather than overwritten.
A moving main rebases the proposed JSON; fresh PR checks prove admissions on the proposed code.

Routine PR checks cover the proposed extension and every changed exclusion, including removals;
unrelated newer upstream releases cannot make them fail. Wiring/code PRs recheck the current policy
window. Excluded contract failures do not admit that release; inconclusive checks still fail CI.

A separate `workflow_run` reporter uses trusted main code and system Python, independent of uv
setup. It files issues for discovery, setup/static-check, timeout, cleanup and publication failures.
It recovers unfinished or failed known versions from qualification, PR verification and canary
logs, then proposes exclusions through the same shared PR. Only identities from this repository's
two expected workflow files can propose exclusions; foreign/fork source failures get a workflow
issue without trusting their claimed versions. Reporting never executes PR code or authorizes
a pass from logs. Concurrency supersession does not fail an unfinished version; completed failures
are still reported, and ordinary cancellations require attention.

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
Distinct parser, safety/recovery and SDK regressions remain supplementary.

## Remaining suite limits

Available OTel channels have no live payload scenario and make qualification inconclusive.
Native child stimulation, timing/usage accounting and foreground MCP timeouts retain explicit
exemptions in `tests/support/coverage.py`; ordinary regressions cover their current behavior.

Hosted sweeps interrupt after 320 minutes, leaving five minutes for cleanup and publication.
Finished outcomes are retained in the shared PR/issues; an unfinished known version is excluded.
OpenCode's 2.0.18–2.0.20 renderer recovery remains independent of policy because baseline 2.0.18
needs it.
