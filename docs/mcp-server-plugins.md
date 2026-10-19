# MCP-server plugins

MCP-server plugins are participant-scoped stdio sidecars rendered into a
participant's harness. They add no tools to Theater itself: the daemon remains
the policy authority and sole SQLite writer.

Put a package at `$THEATER_HOME/plugins/<name>/`:

```text
plugins/
  acme/
    manifest.py
    server.py
```

`manifest.py` exports exactly one immutable `MANIFEST = McpServerManifest(...)`.
The directory name is canonical. A package cannot also export a
`HarnessManifest`; both kinds share the same plugin catalog, and each package
must declare exactly one kind.
`theater plugins` reports both kinds together, including disabled, missing,
broken, and loaded packages.

An enabled plugin may register static Theater skills by canonical name:

```python
MANIFEST = McpServerManifest(
    ...,
    skills=("acme-usage",),
)
```

Each declared skill lives at `skills/<name>/SKILL.md` inside the plugin package.
Theater validates and freezes these files when the daemon loads the plugin;
the sidecar cannot register or change skills at runtime. Registered skills use
the existing `list_skills` and `load_skill` tools and identify their owning
plugin in returned metadata. Names share the global skill namespace. Colliding
plugin registrations are omitted with diagnostics and never override an
existing skill. A missing or malformed declared package makes the enabled
plugin invalid. Disabled plugins contribute no skills and their skill files are
not read. The `[skills] disabled` list in `config.toml` filters only bundled
skills — plugin-registered skills are unaffected by it. Registration needs no
`PluginCapability`; `SKILLS_READ` only lets a
running sidecar consume Theater's skill catalog. Plugins may also register plays.

## Plays

Declare `plays=("acme-play",)` in the manifest and place `PLAY.toml` plus one
`<member>/SKILL.md` per position and role under `skills/acme-play/`.
Discovery validates the complete data-only package. A collision omits the whole
play; no subset of its member skills is registered.

Plays may require enabled plugins and grant permissions per position:

```toml
description = "A tracked team"
master = "master"
plugins = ["beads"]

[positions]
master = ["member"]
member = []

[plugin_permissions.beads]
master = ["read", "write"]
member = ["read"]
```

The dependency and permission tables are pinned when the play starts. Each
plugin declares supported grant names through `play_permissions`; unknown names
are refused. Starting or joining requires enabled, rendered plugin attachments,
and queued assignments are checked again at delivery. An omitted position grants
no permission, including the master. Roles do not change position grants.

With `PluginCapability.PLAYS_READ`, `TheaterPluginClient` exposes:

| Method | Input | Output |
| --- | --- | --- |
| `play_context()` | None | Authenticated `participant_id`, `plugin_name`, `enabled`, and `play` or null |
| `play_members()` | None | Live current-play members: `participant_id`, `name`, `position` |
| `play_instances_status(instance_ids)` | At most 100 instance IDs | `instance_id`, `active` per instance |

Context's `play` contains `instance_id`, `master_id`, `name`, bare `position`,
`plugins`, and the calling plugin's pinned `permissions`. Identity comes from
the participant credential. Missing instances report inactive; inspecting an
existing instance without the plugin dependency is refused. These APIs supply
facts; each plugin owns its tool access policy and may support use outside plays.

With `PluginCapability.JOBS_READ`, `TheaterPluginClient` additionally exposes:

| Method | Input | Output |
| --- | --- | --- |
| `get_job_facts(handles)` | At most 50 exact job handles, each 1–256 nonblank characters | One fact row per handle, order and duplicates preserved |

A row reports `handle` and either `available: false` for a missing or
garbage-collected job, or its facts: `state`, `error_code`, `kind`, `caller_id`,
`target_id`, `created_at`, `finished_at`, `contract_preset`,
`structured_status`, `delegate_status`, `review_verdict`, `git`, `handoff`,
and with `PLAYS_READ` also `play` (`instance_id`,
`caller_seated_at_creation`). Facts stay readable after the job's caller
retires.

The shipped hierarchy play belongs to Beads. Its existing
`[skills] disabled = ["hierarchy"]` opt-out is preserved; local plugin plays
remain unaffected by the bundled-skill denylist.

## Configuration

Enable a sidecar explicitly:

```toml
[mcp]
enabled = ["acme"]

[mcp.plugins.acme]
endpoint = "https://api.example.invalid"
token = { env = "ACME_TOKEN" }
```

Config fields are declared by `McpConfigSchema`. Secret fields accept only
`{ env = "NAME" }` or `{ file = "/path" }`; they are resolved once at startup
and never appear in `theater plugins` output. Capabilities are explicit,
whole-manifest grants: declare the exact `PluginCapability` values needed.
There is no ambient daemon access.

`McpConfigKind.TABLE_LIST` declares a generic array of tables. Give it an
`item_schema`; nested fields use the same scalar, secret, default, and required
rules as top-level fields:

```python
McpConfigSchema({
    "channels": McpConfigField(
        McpConfigKind.TABLE_LIST,
        item_schema=McpConfigSchema({
            "folder_uid": McpConfigField(McpConfigKind.STRING, required=True),
            "token": McpConfigField(McpConfigKind.SECRET, required=True),
        }),
    ),
})
```

```toml
[[mcp.plugins.acme.channels]]
folder_uid = "inbox"
token = { env = "ACME_CHANNEL_TOKEN" }
```

The launch planner returns an `McpLaunchPlan`. Its command, argv, and
environment describe the stdio process; `files` and `private_files` are
relative text artifacts confined to that participant and plugin. Theater
creates a participant-scoped credential file and injects its path through
`THEATER_PLUGIN_CREDENTIAL_PATH`. Theater stores the command, argv, and
environment in a 0600 launch descriptor; the harness sees only a runner and
that descriptor's path. Plugin Python remains trusted: do not copy secrets into
logs or `files`, and prefer the environment or `private_files` over process
arguments that may be visible to the operating system.

`McpLaunchContext.state_dir` is the plugin's private global state directory.
Theater creates it under `var/state/plugins/<name>/`; the plugin chooses its
own filenames and schema:

```python
from theater.mcp_plugins import McpLaunchContext, McpLaunchPlan


def plan(context: McpLaunchContext) -> McpLaunchPlan:
    database = context.state_dir / "state.db"
    return McpLaunchPlan(command="acme-server", env={"ACME_DATABASE": str(database)})
```

Do not expose this location as a machine-specific configuration field. Theater
does not migrate or clean up a plugin's database.

## Native sidecar

A native Python sidecar uses `TheaterPluginClient` directly. It reads the
injected credential for every request and only exposes capability-granted
operations:

```python
from theater.plugin_client import TheaterPluginClient

async def participants():
    async with TheaterPluginClient() as client:
        return await client.list_participants()
```

The client never writes SQLite or tmux and does not autostart a daemon. Handle
authentication, capability, and remote failures as normal sidecar failures;
they are isolated from the harness and other sidecars.

## Compatibility wrapper

An existing MCP server can remain unchanged behind a small wrapper. The wrapper
may invoke the JSON gateway:

```sh
printf '%s' '{"id":"p-123"}' | theater plugin call participants.get
```

The gateway reads the injected credential, accepts exactly one JSON object,
and emits exactly one JSON envelope. It forwards only documented,
capability-scoped plugin operations; it cannot become an arbitrary daemon RPC
tunnel. Use `--credential-file` only for a wrapper or test that needs to pass
the injected path explicitly.

Plugin manifests and launched commands are trusted Python and OS processes:
they run with the daemon user's filesystem, process, and network authority.
The capability credential constrains only authenticated Theater plugin RPCs;
it is not a process, filesystem, or network sandbox. `McpLaunchPlan` confines
the artifact paths Theater writes, not the paths a plugin command can access.
A credential is participant-scoped and revoked when that participant is
cleaned up. Capability grants do not bypass Theater's normal identity, rails,
human-presence, or runtime authorization checks. A broken local package is
diagnostic and non-fatal; an enabled broken package shipped by Theater remains
fatal because it is a product defect.

Cross-kind name collisions are different: a harness package and an MCP-server
package may not share a canonical directory name. Classification reserves every
package name, including MCP packages that are not enabled. A name in
`[harness].disabled` is skipped before import unless `[mcp].enabled` also names
it. Rename or remove either package; `theater plugins` marks the conflict.

## Beads

The bundled Beads plugin is enabled by default and provides `hierarchy`.
Install its CLI separately using the [official installation methods](https://github.com/gastownhall/beads/blob/v1.3.1/README.md):

```sh
brew install beads
# Or:
npm install -g @beads/bd
```

The adapter is qualified against Beads v1.3.1. Tools and schemas load without
`bd`; an authorized board operation reports installation errors if it is missing.
Theater never downloads or installs the runtime.

```toml
[mcp]
enabled = ["beads"] # Default; [] disables all MCP plugins.

[mcp.plugins.beads]
board_ttl_seconds = 604800 # Seven days; minimum 0.
# bd_path = "/opt/homebrew/bin/bd" # Otherwise use PATH.
```

Explicit enablement lists are authoritative. Apply changes by restarting the
daemon and starting new agent sessions. Tools attach statically; each call checks
current membership and its pinned position grants. Loading a role or having an
ancestor in a play grants no access. Hierarchy grants `read` and `write` to its
master, lead, and member positions.

| Tool | Input | Output |
| --- | --- | --- |
| `list_tasks` | `view` (all/ready/mine), optional `status`, `kind`, `cursor`; `limit` 1–100 | `play_instance_id`, task summaries, `next_cursor` |
| `get_task` | `task_id`, `view` (detail/progress, default detail) | Detailed or progress task with `play_instance_id` |
| `create_task` | `title`, optional `description`, `kind`, `priority`, `parent_id`, `assignee`, `dependencies` | Created detailed task |
| `update_task` | `task_id`, optional title/description/kind/priority/status/assignee/parent/note | Updated detailed task |
| `set_dependency` | `task_id`, `prerequisite_id`, `add` (default true) | Play instance, both IDs, edge `present` |
| `claim_task` | `task_id` | Task assigned to authenticated caller, in progress |
| `close_task` | `task_id`, required `outcome` text | Closed detailed task |
| `link_task_job` | `task_id`, `handle`, `role` (work/review), optional `review_of` | `duplicate`, `generation`, `links_total` |

`read` permits list/get; `write` permits the five mutations. Tasks have kinds
`task`, `milestone`, `epic`, priorities 0–4, and statuses `open`, `in_progress`,
`blocked`, `closed`. Detailed tasks report ownership, readiness, dependencies,
blockers, children, notes, and outcomes. Empty `assignee` or `parent_id` in an
update clears that field; omitted fields stay unchanged.

Writers may edit any task on their own board. Assignees must be live members of
the same play; claims require ready tasks unassigned or assigned to the caller.
Recover dead assignees by explicit reassignment. Only `close_task` closes work;
every closed prerequisite resolves its dependency, including failure or
cancellation. Reopen with `update_task(status="open")`, reopening closed parents
and dependent tasks first. Previous outcomes remain in history. Use separate
implementation and review tasks, both required by their milestone.

`link_task_job` records that this play dispatched a job as a task's work or
review. The handle must be an exact job handle (at most 256 characters); the job
must be available, carry a play proof for this instance, have been issued by a
member seated in the play, and have a usable creation time. A review link names
one of the task's work links in `review_of`; a work link names none. An
identical stored link is a no-op duplicate, safe to retry after a transport
failure and costing no facts read, while changing a stored link is refused. A
task keeps at most 100 links. Closing and reopening a task starts a new
generation bounded by the close time — the native `closed_at`, or the reopen
moment when that is unknown: jobs created before the boundary are stale and
refused, and a closed task accepts no new work links until reopened. Links,
generations and boundaries persist in task metadata only.

`get_task(view="progress")` adds the compact link view: generation, boundary and
whether it fell back, the current work and latest review with their states and
evidence (`matched`, `stale`, `unverified`, `unavailable`), and bounded linked
job states. Facts are read in batches of 50, blockers are capped at 200 with
`truncated` reported, and the default `detail` view never reads job facts.

Each play instance gets one lazily initialized embedded board at
`$THEATER_HOME/var/state/plugins/beads/plays/<instance_id>/`, shared across its
members' worktrees. Tools accept no board selector or cross-board references.
Operations serialize across sidecars and recheck authorization under the lock.

Master death ends access. Cleanup records the first authoritative inactive
observation and deletes the board after its TTL. It sweeps at sidecar startup
and periodically, preserves live boards, and skips deletion if Theater cannot
verify liveness. Offline cleanup waits for the next sidecar startup. Closing a
root task records the outcome without ending membership or starting retention.
