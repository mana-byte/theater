"""Tool bodies, kept free of any MCP framework so they can be tested directly.

Identity resolution happens here, once, on the first call. It is deliberately
lazy: the daemon may not exist yet when the harness starts this process, and
failing at import time would show up to the user as "MCP server crashed" with no
explanation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from theater.client import DaemonClient


@dataclass(slots=True)
class Session:
    """One MCP server process, acting on behalf of exactly one participant."""

    participant_id: str | None
    harness: str
    client: DaemonClient
    _resolved: bool = False

    async def identify(self) -> dict:
        """Announce ourselves to the daemon and cache the resulting record.

        `pane` is read from $TMUX_PANE, which usually is not there: the MCP SDK
        replaces the inherited environment with a six-variable allowlist unless
        the harness config says otherwise. When it is missing and no id was
        given on argv, the daemon files us as External — correct, since without a
        pane nobody can type into us.
        """
        record = await self.client.call(
            "hello",
            id=self.participant_id,
            harness=self.harness,
            pane=os.environ.get("TMUX_PANE"),
            cwd=str(Path.cwd()),
        )
        assert isinstance(record, dict)
        self.participant_id = record["id"]
        self._resolved = True
        return record

    async def me(self) -> dict:
        if not self._resolved:
            return await self.identify()
        record = await self.client.call("participants.get", id=self.participant_id)
        assert isinstance(record, dict)
        return record


def _summarise(p: dict) -> dict:
    """Trim a participant record to what another agent needs to route work.

    Everything here answers one of: who are you, where are you, can I reach you.
    ``session_id`` is the harness's opaque identifier for resuming a session;
    it remains None until the observer discovers the participant's transcript.
    The ``name`` field is None for dead participants — names are live-only
    aliases, recyclable across deaths. The ``id`` is the stable reference for
    as long as the row is retained (dead rows are eventually deleted by
    retention GC); use it, not the name, for any targeting that spans time
    or has destructive consequences.
    """
    return {
        "id": p["id"],
        "name": p["name"],
        "harness": p["harness"],
        "tier": p["tier"],
        "status": p["status"],
        "cwd": p["cwd"],
        "branch": p["branch"],
        "session_id": p["session_id"],
        "parent_id": p["parent_id"],
        "addressable": p["addressable"],
    }


async def whoami(session: Session) -> dict:
    return _summarise(await session.me())


async def list_participants(session: Session, *, include_dead: bool = False) -> list[dict]:
    if not session._resolved:
        await session.identify()
    rows = await session.client.call("participants.list", include_dead=include_dead)
    assert isinstance(rows, list)
    me = session.participant_id
    return [{**_summarise(p), "is_self": p["id"] == me} for p in rows]


async def harnesses(session: Session) -> list[dict]:
    """What `spawn_session` will accept, asked of the daemon that has to honour it.

    Filtered to the rows an agent can act on: a harness whose binary is not on
    PATH, or a plugin that failed to load, would be a spawn the daemon refuses.
    Those belong in `theater harnesses`, where a human can fix them; offering
    them here only invites a call that cannot work.

    The daemon is asked rather than the local registry read, because the daemon
    reads its config once at start-up. After a config edit the two disagree, and
    the one that spawns is the one worth believing.
    """
    rows = await session.client.call("harnesses")
    assert isinstance(rows, list)
    return [
        {"name": r["name"], "icon": r["icon"], "binary": r["binary"]}
        for r in rows
        if r["installed"] and not r["error"]
    ]


async def models(session: Session) -> list[dict]:
    """What `spawn_session` will accept as `model`, per harness.

    Asked of the daemon for the reason `harnesses` is: the allowlist is enforced
    from the config the daemon read at start-up, so after an edit the file and
    the running process disagree, and the one that refuses the spawn is the one
    worth reporting. `theater models` answers the same question from the file,
    which is the right answer for the human editing it and the wrong one here.

    Filtered like `harnesses`, and to the same end: every row left is a harness
    a `spawn_session` call could name, so nothing here invites a call that
    cannot work.
    """
    rows = await session.client.call("models")
    assert isinstance(rows, list)
    return [
        {
            "harness": r["harness"],
            "models": r["models"],
            "supported": r["supported"],
            "reasoning": r.get("reasoning", []),
            "reasoning_supported": r.get("reasoning_supported", False),
        }
        for r in rows
        if r["installed"] and not r["error"]
    ]


async def spawn_session(
    session: Session,
    *,
    harness: str,
    prompt: str | None = None,
    response_format: dict | None = None,
    approval: str,
    cwd: str | None = None,
    worktree: str | bool | None = False,
    base_branch: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    resume: str | None = None,
) -> dict:
    """Create a child agent in a new tmux window and return its record.

    The prompt is delivered on the child's argv, not by typing into its pane, so
    this path does not depend on keystroke injection working at all.

    If `worktree` is True, a git worktree is created for the child so it has
    its own isolated index and HEAD. The branch name `theater/<child-id>` is
    reported in the result so the parent can merge it explicitly.

    If `worktree` is a non-empty string, a named shared linked worktree is
    created or joined. Multiple live children spawned with the same name in
    the same canonical main repository run in the same directory and on the
    same branch. This is an expert-mode collaboration primitive, not
    filesystem or Git isolation: the index and HEAD are shared, concurrent
    ``git add``/``commit`` operations can interfere, and the KV store does
    not make file claims atomic or enforce ownership. The branch name
    `theater/named/<name>` is reported in the result.

    `model` is passed to the harness unchanged, but not unchecked: it must be
    one the config lists for that harness, and the harness must be able to
    select a model at all. Both are refused before anything is created; `models`
    reports them. Membership is the whole of the check — Theater cannot confirm
    the name is real or that the CLI honoured it, so a typo the user vouched for
    surfaces as a child on the wrong model, not as an error here.

    `reasoning_effort` mirrors `model`: passed to the harness unchanged, gated
    by the same two checks (adapter capability + config allowlist). Omit it and
    the harness uses its own default. The values a harness accepts are
    CLI-specific (codex: none/minimal/low/medium/high/xhigh/max/ultra; claude:
    low/medium/high/xhigh/max/auto); Theater checks membership in the
    `[reasoning]` allowlist and nothing else.

    `resume` takes a session id from `recall` — or a Theater participant id —
    and attaches the new job to that existing harness session instead of
    starting cold. If the value matches a retained participant, the daemon
    resolves it to that participant's harness session id internally.
    Refused up front
    for a harness whose `plan_launch` has no `resume` parameter. Some
    harnesses accept resume but cannot carry a prompt through it — see
    the tool description for the harness-specific behaviour.

    `response_format` is a JSON Schema hint the daemon adds to the prompt
    guidance exactly once. Theater stores it and later parses the whole
    final assistant answer with ``json.loads``. It does not validate the
    schema, scrape JSON from prose, strip fences, coerce types, or retry.
    The dict is forwarded unchanged; MCP neither serializes nor injects it.
    Resume plus `response_format` is refused for harnesses whose resume path
    cannot carry a prompt.

    The returned ``session_id`` is populated asynchronously by the observer,
    so it is normally None for a newly spawned child. Re-list participants
    later to retrieve it after Theater has attached to the child's transcript.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call(
        "spawn",
        harness=harness,
        prompt=prompt,
        approval=approval,
        cwd=cwd or str(Path.cwd()),
        parent_id=session.participant_id,
        tmux_session=os.environ.get("THEATER_TMUX_SESSION"),
        worktree=worktree,
        base_branch=base_branch,
        model=model,
        reasoning_effort=reasoning_effort,
        resume=resume,
        response_format=response_format,
    )
    assert isinstance(record, dict)
    return _summarise(record)


async def register_pane(session: Session, *, pane: str) -> dict:
    """Adoption fallback: the agent looked up its own $TMUX_PANE and tells us.

    Needed because the MCP environment allowlist hides TMUX_PANE from this
    process. An agent can still read it from its own shell tool. The returned
    ``session_id`` may remain None until the observer discovers the transcript.
    """
    record = await session.client.call(
        "hello",
        id=session.participant_id,
        harness=session.harness,
        pane=pane,
        cwd=str(Path.cwd()),
    )
    assert isinstance(record, dict)
    session.participant_id = record["id"]
    session._resolved = True
    return _summarise(record)


async def await_sessions(
    session: Session, *, handles: list[str], max_wait: float = 150.0
) -> list[dict]:
    """Wait for spawned child sessions to finish, up to max_wait seconds.

    Blocks until ANY of the requested handles reaches a terminal state
    ("done", "crashed", "killed") or max_wait expires, whichever comes first.
    If any handle is already terminal when the call arrives, returns
    immediately — it does not wait for all handles.

    Returns one entry per requested handle with its current state. Entries
    that have reached a terminal state are ready to process; the rest come
    back with state="running" and can be re-awaited in a subsequent call to
    keep waiting for them.

    This blocks the current MCP request only; the daemon and every other
    participant continue running.

    An unknown handle is rejected rather than quietly omitted, and `max_wait`
    is capped daemon-side; a caller that wants longer awaits again.

    `prompt` and `result` are dropped from the agent-facing shape. The prompt
    is what the caller already sent, and `result` was only ever a 2000-char
    clip of the child's own turn; an agent that wants what the child said or
    did reads the transcript directly, which returns it whole. See
    `read_transcript`.
    """
    if not session._resolved:
        await session.identify()
    # Identify the caller so the daemon can refuse an await that would
    # deadlock — waiting on a participant that is, right now, waiting on you.
    jobs = await session.client.call(
        "jobs.await",
        handles=handles,
        max_wait=max_wait,
        caller_id=session.participant_id,
    )
    assert isinstance(jobs, list)
    return [{k: v for k, v in job.items() if k not in ("prompt", "result")} for job in jobs]


async def send_prompt(
    session: Session, *, target_id: str, prompt: str, response_format: dict | None = None
) -> dict:
    """Send a prompt to an already-running agent via tmux send-keys.

    The prompt is typed directly into the target's pane. The target must
    be addressable (Spawned or Adopted). If a human is present at the
    target pane, the call fails with `human_present` — never inject into
    a session a human is using. If the target is already processing a
    send prompt, the call fails with `busy`.

    ``target_id`` may be a participant id or a name. Names work only
    while the participant is live; a dead participant's name is null and
    cannot be resolved. Because names are recyclable, use the id for any
    targeting that spans time or has destructive consequences — a
    recycled name can identify a successor.

    `response_format` is a JSON Schema hint the daemon adds to the prompt
    guidance exactly once. Theater stores it and later parses the whole
    final assistant answer with ``json.loads``. It does not validate the
    schema, scrape JSON from prose, strip fences, coerce types, or retry.
    The dict is forwarded unchanged; MCP neither serializes nor injects it.

    Returns a job handle that can be passed to `await_sessions`.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call(
        "send",
        target=target_id,
        prompt=prompt,
        caller_id=session.participant_id,
        response_format=response_format,
    )
    assert isinstance(record, dict)
    return record


async def store_put(session: Session, *, namespace: str, key: str, value: str) -> dict:
    """Store an exact string in the caller's shared sibling scratchpad.

    Values are exact strings: Theater does not parse, merge, or normalise them.
    Writes are last-writer-wins. The daemon scopes access to the caller's spawn
    tree intersected with the canonical main repo, so this is a short-lived
    scratchpad for siblings coordinating inside git, not durable storage and
    not available outside a git repository.
    """
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "store.put",
        namespace=namespace,
        key=key,
        value=value,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result


async def store_get(session: Session, *, namespace: str, key: str) -> dict:
    """Read an exact string from the caller's shared sibling scratchpad.

    Values are returned exactly as stored, or null when absent. The namespace
    and key are scoped by the daemon to the caller's spawn tree intersected
    with the canonical main repo. This is a short-lived sibling scratchpad
    inside git, with last-writer-wins semantics.
    """
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "store.get",
        namespace=namespace,
        key=key,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result


async def checkpoint(session: Session, *, name: str, notes: str | None = None) -> dict:
    """Create an explicit cumulative snapshot for this participant's delegations.

    A checkpoint is agent-initiated, not an automatic execution checkpoint.
    The daemon records every job delegated by this participant up to this
    point so a later recovery read can compare the recorded snapshot with live
    current state.
    """
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "checkpoint.create",
        name=name,
        notes=notes,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result


async def recovery_read(session: Session, *, checkpoint_id: int) -> dict:
    """Read a checkpoint plus the live state now visible to the caller.

    Returns the checkpoint metadata, recorded snapshot, current live state, and
    any pruned handles so the caller can see what changed since the explicit
    checkpoint was created.
    """
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "checkpoint.read",
        checkpoint_id=checkpoint_id,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result


async def list_checkpoints(session: Session, *, limit: int = 100) -> list[dict]:
    """List the caller's checkpoints, newest first.

    Returns summaries only — call ``recovery_read`` with a checkpoint id for
    the full snapshot and live comparison. Notes are truncated to a preview;
    ``notes_truncated`` in each row flags it.
    """
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "checkpoint.list",
        caller_id=session.participant_id,
        limit=limit,
    )
    assert isinstance(result, list)
    return result


async def recovery_restore(
    session: Session, *, checkpoint_id: int, approval: str
) -> dict:
    """Prepare the checkpoint creator for restoration.

    For a dead parent: spawns or resumes it as a child of the caller.
    For a live parent: reuses it in place (lineage is not changed).
    In both cases, returns the recorded jobs. This is a two-step
    handoff: the parent is prepared, then the caller delivers recovery
    instructions via ``send``.

    The checkpoint is atomically claimed before any side effect and marked
    restored on success or failed on error. A second restore is refused
    with a state-specific error code.

    Approval is required — there is no default.

    Returns the restored parent's new participant id, the action taken
    (``live``, ``resumed``, or ``respawned``), and the recorded jobs.
    """
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "checkpoint.restore",
        checkpoint_id=checkpoint_id,
        approval=approval,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result


async def put_child_back_in_the_wound(session: Session, *, target_id: str) -> dict:
    """Kill a child agent that the caller spawned.

    The permission check lives in the daemon: ``participant.kill`` refuses
    unless the target's ``parent_id`` equals ``caller_id`` — but only for
    callers that send a ``caller_id``, which this tool always does. The CLI
    and the régie deliberately send none and are treated as the operator,
    so an agent that shells out to ``theater kill`` bypasses the check
    entirely. This body is a thin pass-through that adds ``caller_id``,
    exactly as ``send_prompt`` does — so the gate covers this MCP path,
    and nothing else.

    ``target_id`` may be a participant id or a name. Names work only while
    the participant is live; a dead participant's name is null and cannot
    be resolved. An already-dead kill is a no-op, but only when addressed
    by id — a dead participant has no name to resolve. Because names are
    recyclable, use the id for destructive targeting: a recycled name can
    identify a successor.

    **Side effect: destroying a worktree child erases uncommitted work.**
    If the child was spawned with ``worktree=True`` (a unique isolated
    worktree), killing it removes the git worktree and deletes its
    branch. Commits already made on the branch are lost with the branch;
    uncommitted changes in the worktree are lost irreversibly.

    If the child was spawned with ``worktree="<name>"`` (a named shared
    linked worktree), the directory is removed on the last live
    participant's teardown but the **shared branch is always retained** —
    other participants may already have completed work on it. After the
    last teardown the branch remains, and the name cannot be recreated
    until the retained branch is integrated as appropriate and deleted by
    the user.

    This is the daemon's behaviour, not a choice this tool makes, but it
    is the one fact the caller must know before calling — there is no
    confirmation prompt, and no undo.
    """
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "participant.kill",
        id=target_id,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result


async def read_transcript(session: Session, *, target_id: str, last_n: int = 5) -> dict:
    """Read the transcript of a participant, returning full unclipped text.

    The agent-facing await_sessions reply drops prompt and result text.
    This method returns the full assistant responses from the transcript on
    disk, so a caller that needs the complete text can get it.

    ``target_id`` may be a participant id or a name. Names work only
    while the participant is live; a dead participant's name is null
    and cannot be resolved. Use the id to read the transcript of a dead
    participant — the id is the stable reference for as long as the row
    is retained (historical access is retention-bounded; dead rows are
    eventually deleted by GC).

    Returns the last `last_n` events (user, assistant, tool_call,
    tool_result) from the transcript, in chronological order. Each entry
    has `role`, `text` (full, unclipped), `tool_name`, and `turn_end`.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call("read_transcript", id=target_id, last_n=last_n)
    assert isinstance(record, dict)
    return record


async def recall(session: Session, *, paths: list[str], depth: int = 5) -> dict[str, dict]:
    """Per-file timelines of what Theater watched happen.

    Returns one timeline per path, keyed by the repo-relative path.
    Each job point carries a ``session_id`` that composes with
    ``spawn_session(resume=<session_id>)`` — the session id out of
    ``recall`` goes straight into ``resume``.

    Paths may be absolute or repo-relative; they are normalised to
    repo-relative before querying, since that is how they are stored.
    A path that has never been touched comes back as an empty timeline.
    """
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "recall",
        paths=paths,
        depth=depth,
        caller_cwd=str(Path.cwd()),
    )
    assert isinstance(result, dict)
    return result


async def recall_read(session: Session, *, segment_id: str) -> dict:
    """Explain one point of a `recall` timeline.

    Takes the ``segment`` id off a timeline point. A job segment comes
    back with the job's transcript; a gap segment comes back with the
    commits git can find for that blob transition, or an explicit
    ``explained: false`` when it can find none — which is a different
    answer from an empty list.

    Separate from `recall` because it is the only call in the feature
    that spends a ``git log``, and because it answers about one segment
    rather than about paths.
    """
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "recall_read",
        segment_id=segment_id,
        caller_cwd=str(Path.cwd()),
    )
    assert isinstance(result, dict)
    return result
