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
from theater.constants.daemon import RPC_DEFAULT_MAX_WAIT_SECONDS
from theater.constants.harness import HARNESS_MCP_SERVER_NAME
from theater.harness import describe
from theater.mcp import tools
from theater.mcp.tools import Session

#: `spawn_session`'s description — harness list from local registry (hint); daemon is authoritative.
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
response_format: optional JSON Schema hint for prompt guidance only. Pass a
          JSON object or null. The daemon serializes and injects that guidance
          exactly once; MCP forwards the object unchanged and does not inject
          prompt text. After the job finishes, Theater parses the whole final
          assistant answer with json.loads. It performs no schema validation,
          JSON scraping, fence stripping, type coercion, or retry.
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
reasoning_effort: reasoning/thinking effort for the child (e.g. "low",
          "medium", "high"). Optional; omit it and the harness uses its
          default. Mirrors `model`: the config must list the effort for that
          harness under [reasoning], and the adapter must accept it. Not all
          harnesses support it — call list_models for `reasoning_supported`.
          Theater checks membership and nothing else.
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
          If a non-empty string, create or join a named shared linked
          worktree. Multiple live children spawned with the same name
          in the same canonical main repository run in the same
          directory and on the same branch. This is an expert-mode
          collaboration primitive, not filesystem or Git isolation:
          the index and HEAD are shared, concurrent git add/commit
          operations can interfere, and the KV store does not make
          file claims atomic or enforce ownership. The branch name
          theater/named/<name> is in the result. base_branch applies
          only when the named worktree is first created; a later join
          may omit it or repeat the exact same value, and a conflicting
          value is refused. On the
          last live participant's teardown the directory is removed but
          the shared branch is always retained — other participants may
          have completed work on it. After the last teardown the branch
          remains, and the name cannot be recreated until the retained
          branch is integrated as appropriate and deleted by the user.
          Cannot be combined with resume.
base_branch: the branch to base the worktree on. Defaults to current HEAD.
resume:    a session id, from `recall`, to resume instead of starting cold.
           Also accepts a Theater participant id: if the value matches a
           retained participant, the daemon resolves it to that
           participant's harness session id internally. Participant-id
           matches take precedence over native session ids. The harness
           must support it (call list_harnesses; a harness that
           cannot is listed but will refuse the spawn with a message naming
           itself). Some harnesses accept resume but cannot carry a prompt
           through it: opencode's `-s` routes to the session view and drops
           `--prompt`, so resuming opencode with a prompt is refused. Resume
           without a prompt and use send to deliver the task. Resume with
           response_format is refused for harnesses whose resume cannot carry
           a prompt, because the JSON guidance is delivered through the same
           prompt channel. Resume is also refused for live trusted owners and
           for `transcript_identity_lost`; recover by inspecting
           `theater candidates <id>` and rebinding with
           `theater bind <id> <candidate> --confirm-id <id>`.

The returned participant record includes `session_id`, the harness's opaque
resume identifier. It is normally null at spawn time because the observer
discovers it asynchronously; call list_participants later to retrieve it.
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
    mcp = MCPServer(HARNESS_MCP_SERVER_NAME, instructions=INSTRUCTIONS)

    @mcp.tool()
    async def whoami() -> dict:
        """Identify yourself: your id, harness session id, tier, cwd and status.

        Call this before addressing anyone else, so you can tell yourself apart
        from the other participants in the list. `session_id` is the harness's
        opaque resume identifier and may be null until Theater's observer finds
        your transcript; call this tool again later to refresh it.
        """
        return await tools.whoami(session)

    @mcp.tool()
    async def list_participants(
        include_dead: bool = False,
        ids: list[str] | None = None,
        children_only: bool = False,
        limit: int | None = None,
        after_id: str | None = None,
    ) -> list[dict]:
        """List every agent on this machine that Theater knows about.

        Each entry says who they are (harness, id, session_id), where they are
        (cwd, branch), how they got here (tier, parent_id) and whether you can
        send work to them (addressable). `session_id` is the harness's opaque
        resume identifier; it may be null until Theater's observer discovers
        the transcript, so list again later if you need it. External
        participants can call out but can never be called: they have no pane
        to deliver into.

        Names are live-only aliases: a dead participant's name is null, shown
        as "-" in the CLI. Names are recyclable — after a death, a later
        participant can pick up the same mask. The id is the stable reference
        for as long as the row is retained (dead rows are eventually deleted
        by retention GC, so historical access is retention-bounded). Use it,
        not the name, for any targeting that spans time or has destructive
        consequences, because a recycled name can identify a successor.

        `ids` is an optional list of participant ids to fetch — real ids only,
        not names (names are live-only, recyclable aliases). Pass it when you
        need a small number of rows and do not want to pay for the whole table.
        An empty list returns an empty result immediately. Unknown ids are
        silently omitted; diff the requested list against the returned rows if
        you need to know which were absent. `ids` composes with `include_dead`:
        a dead participant is only returned when `include_dead=True`, even when
        its id is listed explicitly.

        Set `children_only=True` to list only participants spawned directly by
        you. It excludes deeper descendants and composes with both `ids` and
        `include_dead`.

        An unfiltered `include_dead=True` listing returns at most 100 rows by
        default. Pass `limit` (1 through 200) to choose a page size, then use
        the final row's stable id as `after_id` for the next page; an empty
        page ends pagination. The order is oldest first. If a cursor was
        deleted by retention GC, restart from the first page. `limit` and
        `after_id` cannot be used with `ids`.

        Each row includes `resume_state`, which exposes the verdict from the
        generic identity and capability gates that `spawn_session(resume=...)`
        checks before delegating to harness-specific resume validation. Values:
        `resumable` (generic gates passed; harness-specific validation in
        `resume_launch_overlay` may still refuse), `live` (participant is still
        running), `no_session_id` (Theater has not recorded the harness session
        id), `harness_cannot_resume` (the harness adapter does not support
        resume), `untrusted` (session id present but transcript provenance is
        below operator-level), `owned_by_live` (a live participant already holds
        a trusted binding for the same session id).
        """
        return await tools.list_participants(
            session,
            include_dead=include_dead,
            ids=ids,
            children_only=children_only,
            limit=limit,
            after_id=after_id,
        )

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

        `reasoning_supported: false` means the adapter cannot select a
        reasoning effort. `reasoning` is the allowlist of efforts the config
        permits for that harness; same semantics as `models`.

        Answered by the daemon, which is the process that enforces the list.
        """
        return await tools.models(session)

    @mcp.tool(description=_spawn_description())
    async def spawn_session(
        harness: str,
        approval: str,
        prompt: str | None = None,
        response_format: dict | None = None,
        cwd: str | None = None,
        worktree: str | bool | None = False,
        base_branch: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        resume: str | None = None,
    ) -> dict:
        return await tools.spawn_session(
            session,
            harness=harness,
            prompt=prompt,
            response_format=response_format,
            approval=approval,
            cwd=cwd,
            worktree=worktree,
            base_branch=base_branch,
            model=model,
            reasoning_effort=reasoning_effort,
            resume=resume,
        )

    @mcp.tool()
    async def register_pane(pane: str) -> dict:
        """Tell Theater which tmux pane you occupy, making you addressable.

        Only needed if `whoami` reports tier "external" while you are in fact
        running inside tmux. Get the value by running `echo $TMUX_PANE` with your
        shell tool; it looks like "%12". The returned `session_id` may be null
        until Theater's observer discovers your transcript.
        """
        return await tools.register_pane(session, pane=pane)

    @mcp.tool()
    async def await_sessions(
        handles: list[str], max_wait: float = RPC_DEFAULT_MAX_WAIT_SECONDS
    ) -> list[dict]:
        """Wait for spawned child sessions to finish.

        Blocks until ANY of the requested handles reaches a terminal state
        ("done", "crashed", "killed") or max_wait expires. If any handle is
        already terminal when the call arrives, returns immediately — it does
        not wait for all handles.

        handles:   the handle values returned by spawn_session (same as the
                   participant id).
        max_wait:  maximum seconds to block. Default 150.

        Returns one entry per requested handle with state ("done", "crashed",
        "running") and error_code. Process the terminal entries; the rest are
        still running and can be re-awaited in a subsequent call. This blocks
        your current request only; the daemon and other agents continue running.

        Keep each wait shorter than your own client's tool timeout, which
        Theater does not set and cannot see. When that timeout is the
        shorter of the two, your call dies on your side while the child
        works on regardless, and what gets you the answer is another
        await, not a longer one.

        "done" means the child's turn ended, not that the work is right. The
        agent-facing reply drops prompt and result text entirely: an agent that
        wants what the child said or did reads bounded transcript pages via
        read_transcript. Continue only with its returned cursor when older
        content is necessary. The transcript is not evidence
        either — before you build on a child's answer, or merge its branch,
        look at what it changed in the repo.
        """
        return await tools.await_sessions(session, handles=handles, max_wait=max_wait)

    @mcp.tool()
    async def send(target_id: str, prompt: str, response_format: dict | None = None) -> dict:
        """Send a prompt to an already-running agent mid-session.

        The prompt is typed directly into the target's tmux pane via
        send-keys. The target must be addressable (Spawned or Adopted).
        The returned handle can be passed to await_sessions.

        target_id: the participant id or its name. Names come from
                   list_participants and work only while the participant
                   is live — a dead participant's name is null and cannot
                   be resolved. Because names are recyclable, a name that
                   pointed at one agent can later point at its successor
                   after a death and respawn; use the id for any targeting
                   that spans time or has destructive consequences.
        prompt:    the text to type into the target's pane.
        response_format: optional JSON Schema hint, guidance only. Pass a
                   JSON object or null. Theater parses the whole final answer with
                   json.loads — no schema validation, fence stripping, or retry.
        Fails with `human_present` (human at the pane), `busy` (target
        processing), `transcript_untrusted` or `transcript_identity_lost`
        (transcript needs binding).
        """
        return await tools.send_prompt(
            session,
            target_id=target_id,
            prompt=prompt,
            response_format=response_format,
        )

    @mcp.tool()
    async def scratchpad_write(value: str, namespace: str, key: str | None = None) -> dict:
        """Append a string entry to the sibling scratchpad; daemon mints the key.

        Returns {"namespace": str, "key": str}. The key is a random short id
        unless you pass one, in which case that entry is updated if it exists
        or inserted if it does not. The daemon scopes access to your
        spawn tree intersected with the canonical main repo, so this is a
        sibling scratchpad inside git, not durable storage and not
        available outside a git repository.

        Use it for small coordination facts: file claims, handoff notes,
        shared design decisions, or breadcrumbs. Do not use it for mutual
        exclusion, queues, durable memory, or source-of-truth records.

        value:     exact string to store.
        namespace: coordination bucket chosen by the agents sharing it.
        key:       optional key to update or insert under; None mints a new id.
        """
        return await tools.scratchpad_write(
            session,
            value=value,
            namespace=namespace,
            key=key,
        )

    @mcp.tool()
    async def scratchpad_get(namespace: str, keys: list[str] | None = None) -> dict:
        """Read entries from the sibling scratchpad.

        Returns {"namespace": str, "entries": {key: value, ...}}. Pass keys
        to fetch specific entries; omit to fetch all entries in the namespace.
        The daemon scopes access to your spawn tree intersected with the
        canonical main repo, so this is not durable storage and is
        unavailable outside a git repository.

        namespace: coordination bucket chosen by the agents sharing it.
        keys:      optional list of entry ids to fetch; None means all.
        """
        return await tools.scratchpad_get(session, namespace=namespace, keys=keys)

    @mcp.tool()
    async def read_transcript(target: str, cursor: str | None = None) -> dict:
        """Read a bounded transcript page and continue toward older content.

        Call once with a known live name or stable id, inspect the newest
        bounded chunk, and use only the returned next_cursor when older
        content is necessary. Stop when the needed event is found; do not call
        list_participants first.

        target: stable participant id or current live name. Dead names are
        cleared and recyclable, so dead reads require the stable id.
        cursor: opaque next_cursor returned by Theater, or null for the
        newest page. Never construct or edit a cursor.

        Returns id, path, chronological event chunks, cursor (the supplied
        cursor), next_cursor, has_more, and truncated. has_more means a
        next_cursor exists; truncated means the response budget cut this
        source page before all eligible events were returned. Event chunks
        retain index, role, tool_name, and turn_end plus UTF-8 byte offsets;
        reaches_text_start is true when a suffix-first chunk includes byte zero.

        Refuses if the transcript needs binding or its identity is lost.
        """
        return await tools.read_transcript(session, target=target, cursor=cursor)

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

        Only a direct child of yours can be killed via this tool. Do not
        bypass this safety policy by shelling out to `theater kill`.

        target_id: the participant id or name of the child to kill. Use the
                   stable child ID; avoid recyclable names for destructive
                   targeting.

        Refuses with `no_self_kill`, `not_your_child`, or `not_found`.
        An already-dead target addressed by ID is a no-op returning {"killed": false,
        "reason": "already_dead"}.

        **Side effect: destroying a worktree child erases uncommitted
        work.** If the child was spawned with worktree=True (a unique
        isolated worktree), killing it removes the git worktree and
        deletes its branch. Commits already made on the branch are
        lost with the branch; uncommitted changes in the worktree are
        lost irreversibly. There is no confirmation prompt and no undo
        anywhere below this tool — the user's yes is the only thing
        standing between a call and lost work, which is why it has to
        be asked for every time.

        If the child was spawned with worktree="<name>" (a named shared
        worktree), the directory is removed on the last live participant's
        teardown but the shared branch is always retained.

        So collect before you kill. Merge the branch, or record the
        commits somewhere outside the worktree, and only then ask. A
        child that has finished still holds its entire output in a
        branch that this call deletes; the natural order — it says it
        is done, so tidy it away — is the order that loses the work.
        """
        return await tools.put_child_back_in_the_wound(session, target_id=target_id)

    @mcp.tool()
    async def recall(paths: list[str], depth: int = 5) -> dict:
        """Who last changed these files, when, and on whose orders.

        Reach for this before editing a file you did not write in this
        session, when a change you cannot account for shows up, or when
        you need to reach the agent behind one. It reads Theater's own
        record of every job that touched the path, so it answers where
        ``git log`` cannot: uncommitted edits, and the identity of the
        agent that made each change.

        One timeline per path, newest first. Each point carries clipped
        task/result, outcome, sha before/after, and a ``segment`` that
        ``recall_read`` expands. Beside the timeline: ``current`` (the
        file's sha now) and ``dirty`` (differs from HEAD). Compare
        ``current`` with the newest point's ending SHA to detect
        post-job changes.

        A gap point is a sha transition no job claims — something
        outside Theater edited the file. ``recall_read`` on its segment
        asks git what.

        ``session_id`` goes straight into
        ``spawn_session(resume=<session_id>)``, so you can put the
        original author back on its own change.

        paths: repo-relative or absolute. Costs the same for 1 path as
               for 40 — ask about every file you care about at once.
        depth: points per path, gaps included. Default 5.

        Scoped to your git root. Worktree children live under it, so their
        edits appear — two worktrees sharing a repo-relative path share
        one timeline, and the jump between them can read as a gap.
        """
        return await tools.recall(session, paths=paths, depth=depth)

    @mcp.tool()
    async def recall_read(segment_id: str) -> dict:
        """Open one point of a recall timeline: the full story behind it.

        Call this when a point's clipped `task`/`result` is not enough
        and you want the agent's actual reasoning, or when a gap point
        needs explaining. Pass the `segment` value from the point.

        Job segment: that job's transcript unclipped, plus every path it
        touched and the shas it moved them between. A transcript no
        longer on disk comes back as unavailable rather than raising —
        everything the database still remembers arrives regardless.

        Gap segment: the commits git can attribute the transition to.
        `explained: false` means git found none, so the edit was never
        committed here — not that nothing happened.

        The only Theater call that spends a `git log`, and it answers
        about one segment: let `recall` narrow first, then spend this on
        the point that matters.
        """
        return await tools.recall_read(session, segment_id=segment_id)

    return mcp


def main(participant_id: str | None = None, harness: str = "unknown") -> None:
    build(participant_id, harness).run(transport="stdio")
