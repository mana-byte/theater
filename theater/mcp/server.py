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
from theater.harness import describe
from theater.mcp import tools
from theater.mcp.tools import Session

#: `spawn_session`'s description, with the harness list left open.
#:
#: A literal here was the bug: it read `"vibe" or "claude"` while the registry
#: had grown codex and opencode, so the two newer adapters were spawnable by
#: every path except the one an agent uses. An agent cannot spawn what its tool
#: schema does not name, and nothing in the daemon was ever going to say
#: otherwise — the schema is the whole of what it knows.
#:
#: Filled from the local registry, which is a hint and not the authority: this
#: process built its registry from the config as it is now, the daemon built
#: its own when it started, and a config edit between the two leaves them
#: disagreeing. `list_harnesses` asks the daemon, and is what the text points
#: at for anything load-bearing.
#: Sent once, in the initialize response. Whether a model ever reads it is the
#: client's decision, and the four harnesses Theater spawns do not make the
#: same one — so nothing load-bearing lives only here. Every directive that
#: changes what a call does is repeated in the description of the tool it
#: applies to, where it is certain to be seen. What is left is the shape of
#: the whole, which belongs to no single tool: the order of the steps, and
#: the two mistakes that only appear once there is more than one child.
INSTRUCTIONS = """Theater makes the other agents on this machine addressable,
so you can hand work to them and collect it back.

The loop: spawn_session to create a child, await_sessions to wait for it,
read_transcript to read what it actually said, the repo to see what it
actually did, a merge to keep the work, and only then ask the user whether
to kill it.

Two failures belong to orchestration rather than to any one call. Children
that share a file do not run in parallel, they produce a conflict; split the
work so each owns files no sibling touches. And a child reports on itself —
"done" is the end of its turn, not a verdict on its work — so check the repo
before you trust it.
"""

SPAWN_DOC = """Start a new agent in its own tmux window as your child.

harness:  which CLI to run. This machine has: {harnesses}. Call
          list_harnesses for the daemon's own answer, which is the one that
          decides whether a spawn succeeds.
prompt:   the task, delivered on the child's command line at startup.
          Optional; omit it or pass null to start a plain CLI with no task.
          The child inherits nothing you know. This string is the entire
          handoff, so it has to carry the goal, the files it may change,
          what it must leave alone, how to check its own work, and what
          to report back. Everything you leave out, it will invent.
approval: "manual" | "edits" | "yolo" — required, no default. This is
          the only thing standing between an unattended child and your
          filesystem, so choose it deliberately. A child you intend to
          await must not be "manual": it stops at its first permission
          prompt with nobody sitting there to answer, and from the
          outside that is indistinguishable from slow work.
cwd:      where the child works. Defaults to your own directory.
model:    which model the child runs, spelled the way its own CLI spells it
          (opencode wants provider/model). Optional; omit it and the harness
          uses its default, which always works. Naming one only works if the
          config allows it for that harness — call list_models for the set,
          which is usually empty until a human writes it. Theater checks
          membership and nothing else: it cannot confirm the CLI honoured the
          name, so a typo someone allowed surfaces as a child on the wrong
          model, not as an error here.
worktree: if True, create a git worktree for the child with its own
          isolated index and HEAD. The branch name theater/<child-id>
          is in the result so you can merge it explicitly. The child's
          repo must be a git repo for this to work.
          What is isolated is the index, not the merge. Two children
          editing the same file are not working in parallel, they are
          writing a conflict you will resolve by hand later. Give each
          one files no sibling touches, and put whatever they share —
          a function signature, a schema — in both prompts, unchanged,
          so the branches still compose when they come back.
base_branch: the branch to base the worktree on. Defaults to current HEAD.
resume:    a session id, from `recall`, to resume instead of starting cold.
           The harness must support it (call list_harnesses; a harness that
           cannot is listed but will refuse the spawn with a message naming
           itself). Some harnesses accept resume but cannot carry a prompt
           through it: opencode's `-s` routes to the session view and drops
           `--prompt`, so resuming opencode with a prompt is refused. Resume
           without a prompt and use send to deliver the task.
"""


def _spawn_description() -> str:
    """SPAWN_DOC with the harness names of this process's registry filled in.

    Every registered adapter is named, including one whose binary is missing:
    `installed` here would be resolved against *this* process's PATH, and the
    daemon spawns with its own. Naming a harness the daemon can in fact spawn
    matters more than hiding one it cannot — the second case fails at the call
    with a message that says so, the first fails silently by never being tried.
    """
    names = [row["name"] for row in describe() if not row["error"]]
    return SPAWN_DOC.format(harnesses=", ".join(names) or "no harnesses registered")


def build(participant_id: str | None = None, harness: str = "unknown") -> MCPServer:
    session = Session(
        participant_id=participant_id or os.environ.get("THEATER_ID"),
        harness=harness,
        client=DaemonClient(),
    )
    mcp = MCPServer("theater", instructions=INSTRUCTIONS)

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
    async def list_harnesses() -> list[dict]:
        """The CLIs you can pass to spawn_session as `harness`, on this machine.

        Answered by the daemon, so it accounts for adapters a user has added
        and for binaries that are not installed. Call it before spawning
        something you have not spawned before: the set is configuration, not a
        fixed list, and it differs between machines.
        """
        return await tools.harnesses(session)

    @mcp.tool()
    async def list_models() -> list[dict]:
        """The models spawn_session will accept for each harness, on this machine.

        An allowlist a human writes in Theater's config, not everything the CLI
        could run, and it starts empty: a harness with `models: []` refuses
        every model you name. That is not a dead end — spawn without `model`
        and the child comes up on whatever its own CLI is set to, which always
        works. Suggest the config edit rather than retrying with another name.

        `supported: false` means the adapter cannot select a model at all, so
        no config edit helps.

        Answered by the daemon, which is the process that enforces the list.
        """
        return await tools.models(session)

    @mcp.tool(description=_spawn_description())
    async def spawn_session(
        harness: str, approval: str, prompt: str | None = None,
        cwd: str | None = None,
        worktree: bool = False, base_branch: str | None = None,
        model: str | None = None,
        resume: str | None = None,
    ) -> dict:
        # Description comes from SPAWN_DOC, since the harness names in it are
        # only known once the registry is built. See `_spawn_description`.
        return await tools.spawn_session(
            session, harness=harness, prompt=prompt, approval=approval, cwd=cwd,
            worktree=worktree, base_branch=base_branch, model=model, resume=resume,
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
        handles: list[str], max_wait: float = 150.0
    ) -> list[dict]:
        """Wait for spawned child sessions to finish.

        handles:   the handle values returned by spawn_session (same as the
                   participant id).
        max_wait:  maximum seconds to block. Default 150. If the timeout
                   expires, jobs still running are returned with state="running"
                   and you should re-await.

        Returns one entry per handle with state ("done", "crashed", "running")
        and error_code. This blocks your current request only; the daemon and
        other agents continue running.

        Keep each wait shorter than your own client's tool timeout, which
        Theater does not set and cannot see. When that timeout is the
        shorter of the two, your call dies on your side while the child
        works on regardless, and what gets you the answer is another
        await, not a longer one.

        "done" means the child's turn ended, not that the work is right. This
        result does not carry the child's response text: read_transcript
        returns what it said in full, and the transcript is not evidence
        either — before you build on a child's answer, or merge its branch,
        look at what it changed in the repo.
        """
        return await tools.await_sessions(
            session, handles=handles, max_wait=max_wait
        )

    @mcp.tool()
    async def send(target_id: str, prompt: str) -> dict:
        """Send a prompt to an already-running agent mid-session.

        The prompt is typed directly into the target's tmux pane via
        send-keys. The target must be addressable (Spawned or Adopted).
        The returned handle can be passed to await_sessions.

        target_id: the participant id of the agent to send to. Use
                   list_participants to find addressable peers.
        prompt:    the text to type into the target's pane.

        Fails with `human_present` if a human is detected at the target
        pane — never inject into a session a human is using. Fails with
        `busy` if the target is already processing a send prompt.
        """
        return await tools.send_prompt(
            session, target_id=target_id, prompt=prompt
        )

    @mcp.tool()
    async def read_transcript(target_id: str, last_n: int = 5) -> dict:
        """Read the transcript of a participant, returning full unclipped text.

        The job result from spawn/send is clipped to 2000 chars. This
        method reads the full transcript from disk and returns the last
        `last_n` events (user, assistant, tool_call, tool_result) with
        complete, unclipped text.

        target_id: the participant id to read.
        last_n:    number of events to return, newest. Default 5. Set to
                   0 for all events in the current transcript.

        Returns {"id": ..., "events": [...], "path": ...}. Each event
        has "role", "text" (full), "tool_name", and "turn_end".
        """
        return await tools.read_transcript(
            session, target_id=target_id, last_n=last_n
        )

    @mcp.tool()
    async def put_child_back_in_the_wound(target_id: str) -> dict:
        """Kill a child agent that you spawned.

        **Ask the user before every call, and wait for an explicit yes.**
        The kill is destructive and cannot be undone, so the decision
        belongs to the user, not to you. Put the question at the end of
        your answer and stop there — for example: "May I clean up the
        child sessions I spawned?" Name the children you mean, because an
        approval covers only those: ask again for a different child, and
        ask again on a later turn. An approval to spawn is not an approval
        to kill, and neither is a general instruction to tidy up.

        Only a direct child of yours can be killed: the daemon enforces
        that the target's parent_id equals your own participant id. This
        check covers every caller that identifies itself — including
        `theater kill` from an agent's shell tool — because it lives in
        the daemon, not in this tool body.

        target_id: the participant id of the child to kill.

        Refuses with `no_self_kill` if the target is you. Refuses with
        `not_your_child` if the target exists but is not your child
        (a sibling, a parent, a stranger, or a grandchild). A target
        that does not exist arrives as `not_found`. A target that is
        already dead is a no-op that returns {"killed": false,
        "reason": "already_dead"} rather than an error — killing a
        dead thing is not a failure.

        **Side effect: destroying a worktree child erases uncommitted
        work.** If the child was spawned with worktree=True, killing
        it removes the git worktree and deletes its branch. Commits
        already made on the branch are lost with the branch; uncommitted
        changes in the worktree are lost irreversibly. There is no
        confirmation prompt and no undo anywhere below this tool — the
        user's yes is the only thing standing between a call and lost
        work, which is why it has to be asked for every time.

        So collect before you kill. Merge the branch, or record the
        commits somewhere outside the worktree, and only then ask. A
        child that has finished still holds its entire output in a
        branch that this call deletes; the natural order — it says it
        is done, so tidy it away — is the order that loses the work.
        """
        return await tools.put_child_back_in_the_wound(
            session, target_id=target_id
        )

    @mcp.tool()
    async def recall(paths: list[str], depth: int = 5) -> dict:
        """What happened to these files, and who can tell me more.

        Returns a per-file timeline of every job that touched the path,
        newest first, with enough on each point to resume the session
        that made it. The ``session_id`` from a timeline point goes
        straight into ``spawn_session(resume=<session_id>)`` — these
        two tools compose.

        Each point carries the harness, the session id, and the segment
        id. A gap point marks a transition no job claims — call
        ``recall_read`` with the segment id to learn what happened
        inside it.

        paths: repo-relative or absolute paths to query.
        depth:  max points per path timeline, after gaps are
                interleaved. Default 5.

        A path that has never been touched returns an empty timeline,
        not an error. The result is keyed by path.
        """
        return await tools.recall(session, paths=paths, depth=depth)

    @mcp.tool()
    async def recall_read(segment_id: str) -> dict:
        """Explain one point of a recall timeline.

        Pass the `segment` value from a point `recall` returned.

        For a job segment you get that job's transcript, plus the paths
        it touched and the shas it moved them between. A transcript that
        no longer exists on disk is reported as unavailable rather than
        raised — everything the database still remembers comes back
        regardless.

        For a gap segment — a sha transition no job claims — you get the
        commits git can attribute it to. If git can find none, that is
        reported as `explained: false` with a note, which means the edit
        was never committed here. It does not mean nothing happened.

        This is the only Theater call that spends a `git log`, so reach
        for it when a timeline has told you *that* something changed and
        you need to know *what*.
        """
        return await tools.recall_read(session, segment_id=segment_id)

    return mcp


def main(participant_id: str | None = None, harness: str = "unknown") -> None:
    build(participant_id, harness).run(transport="stdio")
