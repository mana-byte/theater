# MCP-server plugins

MCP-server plugins are participant-scoped stdio sidecars rendered into a
participant's harness. They add no tools to Theater itself: the daemon remains
the policy authority and sole SQLite writer.

Put a package at `$THEATER_HOME/mcp_servers/<name>/`:

```text
mcp_servers/
  acme/
    manifest.py
    server.py
```

`manifest.py` exports exactly one immutable `MANIFEST = McpServerManifest(...)`.
The directory name is canonical. A package cannot also export a
`HarnessManifest`; harness packages belong under `$THEATER_HOME/harnesses/`.
`theater plugins` reports both kinds together, including disabled, missing,
broken, and loaded packages. Disabled MCP packages are discovered but their
Python is not imported.

Enable a sidecar explicitly and keep configuration flat:

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

The launch planner returns an `McpLaunchPlan`. Its command, argv, and
environment describe the stdio process; `files` and `private_files` are
relative text artifacts confined to that participant and plugin. Theater
creates a participant-scoped credential file and injects its path through
`THEATER_PLUGIN_CREDENTIAL_PATH`. Do not copy that credential into logs,
arguments, or public artifacts.

Plugin Python is trusted. Do not unwrap a `SecretValue` into argv or public
artifacts. When a harness cannot provide a private transport, write the secret
to a 0600 private artifact and place only that artifact's path in the
environment.

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
package may not share a canonical directory name. Every discovered package
reserves its name before import, including disabled packages, so this conflict
prevents daemon startup. Rename or remove one package; `theater plugins` marks
the condition as `conflict`.
