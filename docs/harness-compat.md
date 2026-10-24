# Harness compatibility

Each shipped plugin reads its support policy from its own
`theater/harness/builtin/plugins/<harness>/compatibility.json`:

```json
{"min":"0.163.0","max":"0.164.0","excluded":[]}
```

Bounds are inclusive. The minimum is pinned to the installed CLI baseline; the pipeline changes
the maximum and exclusions. The public
`theater.harness.contracts.version_policy.VersionPolicy` validates and reads the same policy
for runtime gates and tooling. Invalid policies and unsupported releases fail closed.
OpenCode has one plugin and release channel, `opencode` (`@opencode/cli`); 1.x support is removed.

## Local commands

Qualify exactly one supplied version:

```sh
make compat HARNESS=codex VERSION=0.164.0
# Equivalent:
uv run python -m scripts.harness_compat qualify codex 0.164.0
```

Run every published stable release from the pinned minimum through latest locally:

```sh
make compat HARNESS=codex
```

Development checks on the installed CLI:

```sh
make suite HARNESS=vibe
make suite HARNESS=vibe ARGS="-- -k mcp -x"
make canaries HARNESS=vibe
```

A filtered run cannot authorize a policy change. Local commands publish nothing.

## Qualification

`tests/conformance/live/` is the sole qualification authority. It drives a real daemon,
Régie bridge, private tmux server and unmodified stock CLI through the public frontend API
against a local scripted model. Ordinary parser, safety/recovery and distinct SDK integration
regressions remain supplementary; they do not authorize a gate.

The production manifest determines the obligations: launch approvals, prompt completion and
transcript binding, interrupt, steer or refusal, resume, model/effort selection, MCP tools and
refusal, native edits, declared hook/OTel channels, native route binding, daemon-restart survival,
legacy fallback, queue-followup and settings updates.
`tests/support/coverage.py` maps every declaration to a scenario family or an explicit reason
it cannot be observed live; completeness checks reject unmapped declarations.

Only endpoints, dummy credentials, model catalog and onboarding/trust setup belong in
`CompatManifest.provision`. Native edit/MCP recipes supply CLI-specific tool syntax.
Extra generic mutation choices belong in the plugin's `compat.canaries`; shared scenarios
and assertions contain no harness-name branches.

Each qualification installs **exactly its supplied release** and verifies the reported version.
It runs the proposed code in a disposable copy and home. A policy refusal never substitutes
for behavioral testing: only the disposable copy may temporarily admit the supplied release.
Missing scenarios, unexpected skips, install failures, timeouts and incomplete inventories
authorize nothing. Proven contract violations are red.

Required/inventory documents, mock traffic and CLI/daemon files live only in temporary sandboxes
and are deleted after the run. Qualification never edits the checkout or real home, saves results,
updates fixtures or retains successful-version history. There is no `--keep`, qualification cache,
fixture promotion or evidence artifact.

## Workflow and policy PRs

The daily workflow tests every published stable release from the pinned minimum through latest,
in ascending order, including admitted and excluded releases. It continues after failures and
never skips a version based on an earlier result. Discovery and outcomes belong only to that run.

Passing releases authorize a maximum up to the newest pass. Failed or inconclusive releases below
it must be explicitly excluded, and get issues. Later passes can advance the maximum past a
failure without admitting it. Missing discovery or incomplete results authorize no proposal.

A routine policy PR changes **one file, +1/−1**: one compact JSON line with a trailing newline.
It adds no fixtures, history, evidence, Python code or docs. PR checks qualify the proposed code
again and reject disagreement between the policy and fresh outcomes. Durable workflow outputs
are policy PRs and failure issues.

Canaries run separately when suite wiring changes and weekly. They deliberately break generic
manifest values and require the suite to notice; they are never a release verdict.

## Declared limitations

Native child stimulation has no generic scripted scenario. Timing/usage and decoder payload
details use ordinary parser regressions because the live suite has no independent accounting
oracle. Foreground MCP timeout limits require a multi-minute held call and remain unit-tested.
See the reasoned exemptions in `tests/support/coverage.py`.

OpenCode's renderer recovery for 2.0.17–2.0.20 remains behavior-specific and independent of the
support policy: the installed baseline 2.0.18 still needs it.
