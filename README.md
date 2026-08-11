# theater

A tmux-native orchestration layer for coding-agent CLIs.

Theater lets agents running in different harnesses — Claude Code, Vibe — discover
each other, delegate work to each other, and wait for the results, without any of
them knowing what the others are. It is a daemon, an MCP server, and a TUI that
gathers every session on the machine into one tree.

Status: **phase 1a**, under construction. See `docs/implementation_plan.md`.

## What exists today

```
theater daemon        the per-machine registry
theater ls [--tree]   what is running, and who spawned whom
theater spawn ...     start an agent in a new tmux window
theater mcp --id X    the stdio MCP server each agent runs
```

MCP tools: `whoami`, `list_participants`, `spawn_session`, `register_pane`.

## Requirements

- Python 3.12+
- tmux (hard dependency — there is no inbound delivery path without it)
- at least one of `claude`, `vibe` on PATH

## Design

`docs/init_idea_grilled.md` is the specification and records why each decision
went the way it did. The short version:

- **MCP cannot push.** No server can make an agent take a turn. So MCP carries
  everything outbound, and tmux carries everything inbound.
- **Three tiers.** Spawned and Adopted participants have a pane and can be
  addressed. External ones do not and cannot — that is physics, not policy.
- **Observation reads transcripts,** not screens. `capture-pane` is used only to
  detect a human typing.
