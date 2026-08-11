"""The stdio MCP server every participant runs.

One process per agent. Its whole job is to translate tool calls into daemon RPCs
on behalf of exactly one participant, whose id arrives on argv:

    theater mcp --id <participant-id> [--harness vibe]

argv, not the environment, because the MCP SDK strips everything outside a
six-variable allowlist when it launches a stdio server
(mcp/client/stdio/__init__.py:127). $THEATER_ID is still read as a fallback for
harnesses that do pass their environment through.
"""

from __future__ import annotations

import os

from mcp.server import MCPServer

from theater.client import DaemonClient
from theater.mcp import tools
from theater.mcp.tools import Session


def build(participant_id: str | None = None, harness: str = "unknown") -> MCPServer:
    session = Session(
        participant_id=participant_id or os.environ.get("THEATER_ID"),
        harness=harness,
        client=DaemonClient(),
    )
    mcp = MCPServer("theater")

    @mcp.tool()
    async def whoami() -> dict:
        """Identify yourself: your participant id, tier, working directory and status.

        Call this before addressing anyone else, so you can tell yourself apart
        from the other participants in the list.
        """
        return await tools.whoami(session)

    @mcp.tool()
    async def list_participants(include_dead: bool = False) -> list[dict]:
        """List every agent on this machine that Theater knows about.

        Each entry says who they are (harness, id), where they are (cwd, branch),
        how they got here (tier, parent_id) and whether you can send work to them
        (addressable). External participants can call out but can never be
        called: they have no pane to deliver into.
        """
        return await tools.list_participants(session, include_dead=include_dead)

    @mcp.tool()
    async def spawn_session(
        harness: str, prompt: str, approval: str, cwd: str | None = None,
        worktree: bool = False, base_branch: str | None = None,
    ) -> dict:
        """Start a new agent in its own tmux window as your child.

        harness:  "vibe" or "claude"
        prompt:   the task, delivered on the child's command line at startup
        approval: "manual" | "edits" | "yolo" — required, no default. This is
                  the only thing standing between an unattended child and your
                  filesystem, so choose it deliberately.
        cwd:      where the child works. Defaults to your own directory.
        worktree: if True, create a git worktree for the child with its own
                  isolated index and HEAD. The branch name theater/<child-id>
                  is in the result so you can merge it explicitly. The child's
                  repo must be a git repo for this to work.
        base_branch: the branch to base the worktree on. Defaults to current HEAD.
        """
        return await tools.spawn_session(
            session, harness=harness, prompt=prompt, approval=approval, cwd=cwd,
            worktree=worktree, base_branch=base_branch,
        )

    @mcp.tool()
    async def register_pane(pane: str) -> dict:
        """Tell Theater which tmux pane you occupy, making you addressable.

        Only needed if `whoami` reports tier "external" while you are in fact
        running inside tmux. Get the value by running `echo $TMUX_PANE` with your
        shell tool; it looks like "%12".
        """
        return await tools.register_pane(session, pane=pane)

    @mcp.tool()
    async def await_sessions(
        handles: list[str], max_wait: float = 60.0
    ) -> list[dict]:
        """Wait for spawned child sessions to finish.

        handles:   the handle values returned by spawn_session (same as the
                   participant id).
        max_wait:  maximum seconds to block. Default 60. If the timeout
                   expires, jobs still running are returned with state="running"
                   and you should re-await.

        Returns one entry per handle with state ("done", "crashed", "running"),
        result (the assistant's final response text), and error_code.
        This blocks your current request only; the daemon and other agents
        continue running.
        """
        return await tools.await_sessions(
            session, handles=handles, max_wait=max_wait
        )

    return mcp


def main(participant_id: str | None = None, harness: str = "unknown") -> None:
    build(participant_id, harness).run(transport="stdio")
