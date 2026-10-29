# Harness compatibility

Each plugin owns one compact `compatibility.json`, read by runtime admission and scripts:

```json
{"min":"1.2.3","max":"1.4.0","excluded":["1.3.0"]}
```

Bounds are inclusive. Min is the floor: the sweep raises it to keep at most the newest N releases
in range (N is the `HARNESS_COMPAT_MAX_RANGE` repository variable, default 10), and it never falls.
Exclusions may exceed max inside a ceiling-raising PR. A failed newer release gets a version issue;
an all-failure sweep creates no policy branch or PR. Invalid policies and unsupported binaries fail
closed. OpenCode has one plugin/channel (`@opencode/cli`); 1.x is removed.

## Ownership and flow

Only GitHub workflows invoke `harness_compat`; there are no local qualification targets.

```text
workflow → harness_compat → exact harness/version → generic live suite
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
The workflow's last job, `Failures`, lists the ledgers of every finished job of the run attempt in
its log and the run's step summary, green jobs holding an excluded red version included. Every failed
job lists its failed steps; one with no ledger also shows the output before each error marker. It only
reads and reports: its own failure never fails the run, so keep it out of required checks.

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

`harness_compat/conformance/live/` is the sole qualification authority. Its scenarios derive from the
selected plugin's production manifest and exercise a real daemon, Régie provider, private tmux
server and stock CLI through public contracts against a scripted local model.

Harness-specific onboarding, endpoints, dummy credentials and tool syntax live in the adapter's
`CompatManifest` (see [Adapters](#adapters)). Canary declarations also live there; shared scenarios, assertions and mutations
are generic. Canaries check suite sensitivity separately, weekly and when wiring changes. A
canary run that leaves a needed obligation inconclusive (`error`) is repeated once; a miss never is.

Each qualification installs and verifies **exactly its supplied version**, then runs in a disposable
source copy and home. Only that copy may temporarily admit the candidate. Required/inventory
reports, CLI/daemon files and mock traffic are temporary and deleted after the run. No source
checkout or real home writes, saved results, fixture updates, artifacts or success history.

Only a complete passing suite can advance max. Missing scenarios, skips, invalid inventories,
timeouts, non-clean subprocess exits and cleanup failures cannot authorize a pass.
Cleanup waits for owned processes before deleting data; already-missing child files are harmless.
A cleanup failure retains completed checks and the actual error, stops the sweep, and cannot pass.
Distinct parser, safety/recovery and SDK regressions remain supplementary.

A failed live cell also leaves an evidence block: at teardown the suite writes
`e/blocks/<cell>.txt` — cause first, then job/transcript facts, daemon and bridge log tails, raw
stderr and pane lines, scrubbed of ANSI/control bytes and tokens and capped at 12 KB per cell and
96 KB per release — and the qualification prints those blocks into the job log before the sandbox
is deleted. Blocks are log output only: never report details, never artifacts.

## Adapters

Each shipped plugin has one adapter, `harness_compat/adapters/<name>.py`: the adapter names are the
plugin folders that hold a `manifest.py`, and a unit test holds the two sets equal. Its `ADAPTER`
declares how to `discover()` the stable releases, how to `install()` exactly one of them into a
sandbox, and its `compat` (`CompatManifest`). Modules starting with `_` are shared helpers.

Adding a harness takes its plugin, with a one-line `compatibility.json`, and its adapter; a new kind
of manifest declaration also needs a scenario family or a reasoned exemption in
`harness_compat/support/coverage.py`. No workflow names a harness: `plan` derives every matrix from
the adapters, and a change to one adapter re-verifies only that harness.

`compat` is read only by the live suite, never at runtime. The suite derives every scenario from
the plugin's manifest; `compat` supplies only what cannot be derived, so an adapter declares no
scenarios, assertions, skips or proof callbacks. Constructing a `CompatManifest` or a tool template
refuses a missing placeholder, an oversized name or payload, a repeated canary and a malformed
acknowledged route.

`CompatManifest.provision(context) -> Provisioned` is pure. It receives a
`ProvisionContext` (sandbox directories, the mock model's base URL per protocol,
the binary and its version, and the logical model names the run will request)
and returns the `Provisioned` values that make the stock CLI use the mock:

- `protocol`: `"anthropic"`, `"openai-chat"` (Chat Completions) or
  `"openai-responses"`;
- `env`: endpoint, dummy credential, update and telemetry opt-outs;
- `files`: absolute config, model-catalog and onboarding or trust pre-seeding;
- `models`: selectors the CLI accepts for the logical names, routed to the mock;
- `efforts`: harness-native effort values worth sampling.

`CompatManifest.acknowledged_routes` declares optional `(method, path)` pairs for non-model
side channels. The mock returns 204 for them without consuming a scripted reply; they cannot
replace model protocol routes.

Provisioning must not replace launch argv, approval policy or receipt commands;
production launch planning always wins. Every line of it is endpoint, credential,
model catalog or onboarding and trust setup: a retry policy, a disabled agent or
any other change to stock behaviour is not provisioning.

`CompatManifest.native_edit` is optional and valid only when the plugin's
`launch.approvals` includes `edits` (a unit test checks every adapter). `NativeEdit(name, payload)` names the CLI's
own file-write tool and a payload that makes it write one scratch file: a JSON
object or freeform text (for grammar-based tools such as a patch). The payload
must contain the `{path}` placeholder and may contain `{nonce}`; those two
tokens are substituted in one pass and nothing else is interpolated. The suite
checks the scripted call against the schema the CLI actually offered the tool
with; a call that does not fit (an argument a closed schema does not declare, a
missing required one) means the recipe is stale for that release, and the run is
inconclusive rather than red. One static recipe cannot serve two release lines
that name an argument differently, so declare the line the suite can prove.

`CompatManifest.mcp_call` is optional and needed only when the model cannot call
an MCP tool under its own name, for example when MCP tools sit behind a script
runner. `McpCall(name, payload)` names the tool the model calls
and a payload that invokes one MCP tool; it must contain `{tool}` and
`{arguments}` and may contain `{server}`, substituted in one pass (`{arguments}`
as its JSON text). Without it the suite finds the MCP tool among the tool names
the model was offered by suffix match.

`CompatManifest.mcp_discovery` is optional and needed only when the CLI offers an MCP
tool after a search for it (a tool search). `McpDiscovery(name, payload)` names the
search tool and a payload that loads one MCP tool; it must contain `{tool}` and may
contain `{server}`. The suite runs it before the tool's own call, which it then makes by the
name the CLI offers afterwards (or through `mcp_call`).

`CompatManifest.canaries` names extra generic mutations that this adapter's provisioning cannot
mask. The mutation implementations and assertions stay in the shared suite. These tooling checks
run separately from release qualification; no shared code branches on a harness name.

## Remaining suite limits

Available OTel channels have no live payload scenario and make qualification inconclusive.
Native child stimulation, timing/usage accounting and foreground MCP timeouts retain explicit
exemptions in `harness_compat/support/coverage.py`; ordinary regressions cover their current behavior.

Hosted sweeps interrupt after 320 minutes, leaving five minutes for cleanup and publication.
The sweep also carries a 310-minute time budget: a release that cannot start with at least ten
minutes of sweep time left is recorded inconclusive and published instead of left untested, and
the rest of the sweep is skipped the same way. Three identical tooling errors in a row record
the remaining releases the same way. Skipped releases stay above max and are retested by the
next sweep. Finished outcomes are retained in the shared
PR/issues; an unfinished known version gets its version issue and remains unqualified.
OpenCode's 2.0.18–2.0.20 renderer recovery remains independent of policy because baseline 2.0.18
needs it.

## After a floor raise

A raise ships in a routine ceiling PR: one JSON line, nothing else. Whatever only the releases below
the new min needed is deleted afterwards, in a separate code PR kept inside `plugins/<harness>/`,
`harness_compat/adapters/<harness>.py` and `tests/harness/<harness>/` so that only that harness's
shards run. Add a row when code is written for specific releases; the row dies with its code.

| Dies when min ≥ | Harness | Shape | Delete |
| --- | --- | --- | --- |
| 2.0.19 | opencode | `--prompt` races a cold server on a new session | `launch.py` `_open_new_session` and `_CREATE_THEN_EXEC`, the `test_launch_plan.py` tests that pin them; after a live launch with an initial prompt passes on the new window |
| 2.0.21 | opencode | OpenTUI viewport clipping | `tui_recovery.py`, `assets/tui_recovery.js`, the `tui.js` entry in `native_plugin.py`, their tests and the note above |
| 2.2.0 | opencode | plugin source is `server.js` (2.1.4 and older) | the `generated` file member of the source-path set in `classify_plugin_state` (`approval.py`) and its fixtures |
