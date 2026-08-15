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
    """
    return {
        "id": p["id"],
        "harness": p["harness"],
        "tier": p["tier"],
        "status": p["status"],
        "cwd": p["cwd"],
        "branch": p["branch"],
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
        }
        for r in rows
        if r["installed"] and not r["error"]
    ]


async def spawn_session(
    session: Session,
    *,
    harness: str,
    prompt: str | None = None,
    approval: str,
    cwd: str | None = None,
    worktree: bool = False,
    base_branch: str | None = None,
    model: str | None = None,
    resume: str | None = None,
) -> dict:
    """Create a child agent in a new tmux window and return its record.

    The prompt is delivered on the child's argv, not by typing into its pane, so
    this path does not depend on keystroke injection working at all.

    If `worktree` is True, a git worktree is created for the child so it has
    its own isolated index and HEAD. The branch name `theater/<child-id>` is
    reported in the result so the parent can merge it explicitly.

    `model` is passed to the harness unchanged, but not unchecked: it must be
    one the config lists for that harness, and the harness must be able to
    select a model at all. Both are refused before anything is created; `models`
    reports them. Membership is the whole of the check — Theater cannot confirm
    the name is real or that the CLI honoured it, so a typo the user vouched for
    surfaces as a child on the wrong model, not as an error here.

    `resume` takes a session id from `recall` and attaches the new job to
    that existing harness session instead of starting cold. Refused up front
    for a harness whose `plan_launch` has no `resume` parameter. Some
    harnesses accept resume but cannot carry a prompt through it — see
    the tool description for the harness-specific behaviour.
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
        resume=resume,
    )
    assert isinstance(record, dict)
    return _summarise(record)


async def register_pane(session: Session, *, pane: str) -> dict:
    """Adoption fallback: the agent looked up its own $TMUX_PANE and tells us.

    Needed because the MCP environment allowlist hides TMUX_PANE from this
    process. An agent can still read it from its own shell tool.
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

    Returns the current state of each job. A job that is still running when
    the timeout expires is returned with state="running" — re-await if you
    want to keep waiting. The result text is the assistant's final response
    from the child's turn.

    This blocks the current MCP request only; the daemon and every other
    participant continue running.

    An unknown handle is rejected rather than quietly omitted, and `max_wait`
    is capped daemon-side; a caller that wants longer awaits again.
    """
    if not session._resolved:
        await session.identify()
    # Identify the caller so the daemon can refuse an await that would
    # deadlock — waiting on a participant that is, right now, waiting on you.
    # Without this the rail could only ever see awaits made from the CLI.
    jobs = await session.client.call(
        "jobs.await",
        handles=handles,
        max_wait=max_wait,
        caller_id=session.participant_id,
    )
    assert isinstance(jobs, list)
    return jobs


async def send_prompt(
    session: Session, *, target_id: str, prompt: str
) -> dict:
    """Send a prompt to an already-running agent via tmux send-keys.

    The prompt is typed directly into the target's pane. The target must
    be addressable (Spawned or Adopted). If a human is present at the
    target pane, the call fails with `human_present` — never inject into
    a session a human is using. If the target is already processing a
    send prompt, the call fails with `busy`.

    Returns a job handle that can be passed to `await_sessions`.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call(
        "send",
        target=target_id,
        prompt=prompt,
        caller_id=session.participant_id,
    )
    assert isinstance(record, dict)
    return record


async def put_child_back_in_the_wound(
    session: Session, *, target_id: str
) -> dict:
    """Kill a child agent that the caller spawned.

    The permission check lives in the daemon, not here: ``participant.kill``
    refuses unless the target's ``parent_id`` equals ``caller_id``. This
    body is a thin pass-through that adds ``caller_id``, exactly as
    ``send_prompt`` does — so the gate covers every caller that identifies
    itself, including ``theater kill`` from an agent's shell tool, which a
    body-only check would have missed.

    **Side effect: destroying a worktree child erases uncommitted work.**
    If the child was spawned with ``worktree=True``, killing it removes
    the git worktree and deletes its branch. Commits already made on the
    branch are lost with the branch; uncommitted changes in the worktree
    are lost irreversibly. This is the daemon's behaviour, not a choice
    this tool makes, but it is the one fact the caller must know before
    calling — there is no confirmation prompt, and no undo.
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


async def read_transcript(
    session: Session, *, target_id: str, last_n: int = 5
) -> dict:
    """Read the transcript of a participant, returning full unclipped text.

    The job result from spawn/send is clipped to 2000 chars. This method
    returns the full assistant responses from the transcript on disk,
    so a caller that needs the complete text can get it.

    Returns the last `last_n` events (user, assistant, tool_call,
    tool_result) from the transcript, in chronological order. Each entry
    has `role`, `text` (full, unclipped), `tool_name`, and `turn_end`.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call(
        "read_transcript", id=target_id, last_n=last_n
    )
    assert isinstance(record, dict)
    return record
