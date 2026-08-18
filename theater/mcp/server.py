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
#: Filled from the local registry, which is a hint and not the authority: the
#: daemon built its own when it started, and a config edit leaves them
#: disagreeing. `list_harnesses` asks the daemon for anything load-bearing.
#: The schema is the whole of what an agent knows — nothing load-bearing
#: lives only here; every directive is repeated in the tool it applies to.
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
    mcp = MCPServer("theater", instructions=INSTRUCTIONS)

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

        Each row includes `resume_state`, which exposes the verdict
        `spawn_session(resume=...)` would return for that participant without
        actually attempting the spawn. Values: `resumable` (spawn would work),
        `live` (participant is still running), `no_session_id` (Theater has not
        recorded the harness session id), `harness_cannot_resume` (the harness
        adapter does not support resume), `untrusted` (session id present but
        transcript provenance is below operator-level), `owned_by_live` (a live
        participant already holds a trusted binding for the same session id).
        """
        return await tools.list_participants(session, include_dead=include_dead, ids=ids)

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
    async def await_sessions(handles: list[str], max_wait: float = 150.0) -> list[dict]:
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
        wants what the child said or did reads the transcript via
        read_transcript, which returns it whole. The transcript is not evidence
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
        response_format: optional JSON Schema hint for prompt guidance only.
                   Pass a JSON object or null. The daemon serializes and
                   injects that guidance exactly once; MCP forwards the object
                   unchanged and does not inject prompt text. After the job
                   finishes, Theater parses the whole final assistant answer
                   with json.loads. It performs no schema validation, JSON
                   scraping, fence stripping, type coercion, or retry.

        Fails with `human_present` if a human is detected at the target
        pane — never inject into a session a human is using. Fails with
        `busy` if the target is already processing a send prompt. Fails with
        `transcript_untrusted` for an adopted transcript-backed target that
        still needs an operator/proven/exact transcript binding, and with
        `transcript_identity_lost` when a trusted transcript pin must be
        rebound before attribution can resume.
        """
        return await tools.send_prompt(
            session,
            target_id=target_id,
            prompt=prompt,
            response_format=response_format,
        )

    @mcp.tool()
    async def store_put(namespace: str, key: str, value: str) -> dict:
        """Store one exact string for short-lived sibling coordination.

        Values are exact strings: Theater does not parse, merge, or normalise
        them. Writes are last-writer-wins. The daemon scopes access to your
        spawn tree intersected with the canonical main repo, so this is a
        sibling scratchpad inside git, not durable storage and not available
        outside a git repository. There is no prefix listing in this MCP
        surface.

        Use it for small coordination facts: file claims, sibling handoff
        notes, shared design decisions, or "child X owns slice Y" breadcrumbs.
        Choose naturally unique keys such as repo paths, child ids, or task
        ids so two agents do not silently overwrite each other.

        Do not use it for mutual exclusion, queues, durable memory, project
        state, or source-of-truth records. A stored value is only a note for
        related live agents; commits, transcripts, checkpoints, and repo
        inspection are still the evidence.

        namespace: coordination bucket chosen by the agents sharing it.
        key:       exact key inside that namespace.
        value:     exact string to store.
        """
        return await tools.store_put(
            session,
            namespace=namespace,
            key=key,
            value=value,
        )

    @mcp.tool()
    async def store_get(namespace: str, key: str) -> dict:
        """Read one exact string from the sibling scratchpad.

        Returns {"value": str | null}. Values are exact strings and writes are
        last-writer-wins. The daemon scopes access to your spawn tree
        intersected with the canonical main repo, so this is a short-lived
        scratchpad for siblings coordinating inside git and is unavailable
        outside a git repository. There is no prefix listing in this MCP
        surface.

        Reach for it when a sibling may already have recorded a file claim,
        handoff note, shared assumption, or work-slice ownership. Treat a
        missing value as "no note was found", not as proof that nobody is
        working there; Theater does not provide atomic claims or listing here.

        namespace: coordination bucket chosen by the agents sharing it.
        key:       exact key inside that namespace.
        """
        return await tools.store_get(session, namespace=namespace, key=key)

    @mcp.tool()
    async def checkpoint(name: str, notes: str | None = None) -> dict:
        """Create an explicit cumulative snapshot of your delegated jobs.

        This is not an automatic execution checkpoint; it is agent-initiated
        bookkeeping. It records every job delegated by this participant up to
        now and returns {"checkpoint_id": int, "jobs": [...]}. Use it before
        a risky orchestration turn or handoff when you want a recovery marker.

        It snapshots job records only: handles, targets, prompts, states,
        results, errors, and times. It does not snapshot repo files, tmux panes,
        transcripts, child memory, worktrees, or any filesystem state.

        Checkpoints are visible to every participant on this machine — they are
        not private per-agent state. The snapshot includes job prompts and
        results. Do not assume a checkpoint is private.

        Good moments to create one: before spawning or awaiting a batch of
        children, before context compaction or handoff to another agent, before
        a merge/recovery step, or before leaving long-running delegations
        unattended.

        name:  short label for the checkpoint.
        notes: optional free-form notes for the later reader.
        """
        return await tools.checkpoint(session, name=name, notes=notes)

    @mcp.tool()
    async def recovery_read(checkpoint_id: int) -> dict:
        """Read a checkpoint and compare it with live state now.

        Operates on any participant's checkpoint — not only your own.

        Returns checkpoint metadata plus the recorded snapshot, current live state,
        and any pruned handles. Use it to see what changed since an
        explicit agent-created checkpoint, including jobs that finished,
        moved, disappeared from retention, or now have different structured
        results.

        Useful after an interrupted await, daemon/client restart, context
        handoff, or any point where you need to reconstruct which delegated jobs
        still need transcript reading, repo inspection, another await, or
        cleanup. A pruned handle means the job record aged out or disappeared;
        it is not proof that the underlying work was irrelevant or safely
        collected.

        checkpoint_id: the id returned by checkpoint.
        """
        return await tools.recovery_read(session, checkpoint_id=checkpoint_id)

    @mcp.tool()
    async def list_checkpoints(
        limit: int = 100,
        participant_id: str | None = None,
        restorable_only: bool = False,
    ) -> list[dict]:
        """List checkpoints across all participants on this machine, newest first.

        Checkpoints are machine-global by design: a dead creator's checkpoint must
        be discoverable by a live sibling that will restore it.

        Returns id, participant_id, creator_name, creator_present, name, created_at,
        restore_state, and a truncated notes preview for each checkpoint. Call
        recovery_read with a checkpoint id for the full snapshot and live comparison.

        limit:           maximum number of checkpoints to return (1-100, default 100).
        participant_id:  optional — filter to one creator's checkpoints.
        restorable_only: optional — when true, exclude checkpoints whose
                         restore_state is not 'ready'.
        """
        return await tools.list_checkpoints(
            session,
            limit=limit,
            participant_id=participant_id,
            restorable_only=restorable_only,
        )

    @mcp.tool()
    async def recovery_restore(checkpoint_id: int, approval: str) -> dict:
        """Restore the orchestration tree from a checkpoint.

        Operates on any participant's checkpoint — not only your own. You cannot
        restore your own checkpoint or any checkpoint you appear in (self-restore
        would deadlock the MCP call). Only ``ready`` checkpoints can be claimed;
        ``partial`` and ``failed`` are terminal and cannot be re-attempted.

        For v2 checkpoints (full tree): reconciles each recorded node as one of
        the five public actions: ``reused_live`` (live verified in place),
        ``resumed`` (dead with trusted session resumed), ``respawned`` (cold
        respawn from launch provenance), ``skipped`` (completed work or ancestor
        not restored), or ``failed`` (lineage conflict, no provenance, EXTERNAL).

        restore_state in the result is ``restored`` (all nodes succeeded),
        ``partial`` (some succeeded, some failed), or ``failed`` (creator failed).
        All three are terminal — no retry is possible.

        For v1 checkpoints (degraded mode): creator-only restore, no descendants
        recorded or recovered. The result shape is different and contains
        ``_degraded: true`` to signal the limitation.

        The checkpoint is atomically claimed after preflight (structural, rail,
        and cycle validation). No text is injected during restore; delivery of
        recovery instructions to live participants is a separate ``send`` call.

        Returns a structured report with participants (flat list with
        original/current/new IDs, parent IDs, session IDs, action,
        classification, status, reason, and job reconciliations), summary,
        counts, aggregated warnings, restore_state, and a deduplicated jobs list.

        checkpoint_id: the id returned by ``checkpoint``.
        approval:       ``manual``, ``edits``, or ``yolo`` — no default.
        """
        return await tools.recovery_restore(session, checkpoint_id=checkpoint_id, approval=approval)

    @mcp.tool()
    async def read_transcript(target_id: str, last_n: int = 5) -> dict:
        """Read the transcript of a participant, returning full unclipped text.

        The agent-facing await_sessions reply drops prompt and result text.
        This method reads the full transcript from disk and returns the last
        `last_n` events (user, assistant, tool_call, tool_result) with
        complete, unclipped text.

        target_id: the participant id or its name. Names work only while
                   the participant is live; a dead participant's name is
                   null and cannot be resolved. Use the id to read the
                   transcript of a dead participant — the id is the stable
                   reference for as long as the row is retained (historical
                   access is retention-bounded; dead rows are eventually
                   deleted by GC).
        last_n:    number of events to return, newest. Default 5. Set to
                   0 for all events in the current transcript.

        Returns {"id": ..., "events": [...], "path": ...}. Each event
        has "role", "text" (full), "tool_name", and "turn_end".

        Refuses with `transcript_untrusted` until adopted transcript-backed
        sessions are operator/proven/exact. Refuses with
        `transcript_identity_lost` if a trusted pin lost identity; screen
        status may still be live, but attribution-bearing reads wait for
        `theater candidates <id>` / `theater bind <id> <candidate>
        --confirm-id <id>`.
        """
        return await tools.read_transcript(session, target_id=target_id, last_n=last_n)

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

        Only a direct child of yours can be killed via this MCP tool: the
        daemon checks that the target's parent_id equals your own
        participant id, which this tool always sends. But the check only
        applies to callers that identify themselves. The CLI and the régie
        deliberately send no caller_id and are treated as the operator, so
        an agent that shells out to `theater kill` bypasses the check
        entirely — the parent-child gate does not protect you there.

        target_id: the participant id or name of the child to kill.
                   Names work only while the participant is live; a dead
                   participant's name is null and cannot be resolved. Use
                   the id for destructive targeting: names are recyclable,
                   so a name that pointed at one child can later point at
                   a successor after a death and respawn.

        Refuses with `no_self_kill` if the target is you. Refuses with
        `not_your_child` if the target exists but is not your child
        (a sibling, a parent, a stranger, or a grandchild). A target
        that does not exist arrives as `not_found`. A target that is
        already dead is a no-op — but only when addressed by id, because
        a dead participant has no name to resolve. The no-op returns
        {"killed": false, "reason": "already_dead"} rather than an
        error — killing a dead thing is not a failure.

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
        linked worktree), the directory is removed on the last live
        participant's teardown but the shared branch is always retained
        — other participants may already have completed work on it.
        After the last teardown the branch remains, and the name cannot
        be recreated until the retained branch is integrated as appropriate
        and deleted by the user.

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

        One timeline per path, newest first. Each job point carries:

        - what: ``task`` and ``result`` (clipped to 300 chars),
          ``outcome``, and ``sha`` before → after
        - who: ``harness``, ``session_id``, ``cwd``, ``branch``;
          ``parent_id``/``parent_name`` is whoever spawned that agent
          (``None`` for a root), ``caller_id`` whoever ordered that job
          — for a ``send``, often a sibling rather than the parent
        - where next: ``segment``, which ``recall_read`` expands

        Beside the timeline: ``current`` (the file's sha right now) and
        ``dirty`` (differs from HEAD). Compare ``current`` against the
        newest point's ``sha_after`` to see if anything has moved the
        file since the last job left it.

        A gap point is a sha transition no job claims — something
        outside Theater edited the file. ``recall_read`` on its segment
        asks git what.

        ``session_id`` goes straight into
        ``spawn_session(resume=<session_id>)``, so you can put the
        original author back on its own change.

        paths: repo-relative or absolute. Costs the same for 1 path as
               for 40 — ask about every file you care about at once.
        depth: points per path, gaps included. Default 5.

        Scoped to your git root: a job whose cwd sits outside it is not
        returned. Worktree children live under it, so their edits do
        appear — two worktrees sharing a repo-relative path share one
        timeline, and the jump between them can read as a gap. A path
        Theater never watched returns an empty timeline, not an error.
        Results are keyed by path.
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
