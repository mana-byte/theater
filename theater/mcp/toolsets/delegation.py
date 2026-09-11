"""Spawning, sending, steering, queued followups, settings, and killing.

These are the tools an agent uses to delegate work to other agents and
coordinate with them. ``_summarise`` is imported from the participants toolset
because ``spawn_session`` and ``register_pane`` both project the returned
participant record through it.

The runtime-control bodies below are thin forwarders: the daemon owns every
policy decision (authorization, capability, idle, allowlists) and every
refusal reason, and each call names the calling participant so the daemon can
make those decisions on the real identity.
"""

from __future__ import annotations

import os
from pathlib import Path

from theater.constants.daemon import RPC_DEFAULT_MAX_WAIT_SECONDS
from theater.mcp.session import Session
from theater.mcp.toolsets.participants import _summarise


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
    name: str | None = None,
    description: str | None = None,
    wiring: str = "auto",
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
    and continues that context. Harnesses with native fork support create a
    fresh session identity; older resume-only harnesses reuse the existing one.
    If the value matches a retained participant, the daemon resolves it to
    that participant's harness session id internally.
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

    ``wiring`` selects how the child is wired: "auto" (default), "native",
    or "legacy" (tmux delivery only). The daemon owns rollout and
    compatibility selection: under "auto" an unverified harness — and every
    harness while the native rollout gate is disabled — stays legacy, and
    explicit "native" fails honestly with a diagnostic instead of falling
    back. The choice is forwarded unchanged; the daemon's refusal names the
    reason. It is independent of ``approval``, which remains required with
    no default.
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
        name=name,
        description=description,
        response_format=response_format,
        wiring=wiring,
    )
    assert isinstance(record, dict)
    return _summarise(record)


async def await_sessions(
    session: Session, *, handles: list[str], max_wait: float = RPC_DEFAULT_MAX_WAIT_SECONDS
) -> list[dict]:
    """Forward presence-aware awaits, omitting prompt and result text."""
    if not session._resolved:
        await session.identify()
    # Caller identity lets the daemon reject mutual-await deadlocks.
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
    """Send a prompt to an already-running agent on its selected transport.

    Delivery follows the wiring the daemon selected for the target: the
    native runtime for a native-wired participant, keystroke injection into
    the pane only for legacy wiring. There is no fallback between the two —
    an unknown or disconnected native delivery is reported as such, never
    silently retried into the pane. The target must be addressable (Spawned
    or Adopted). If a human is present at the target pane, the call fails
    with `human_present` — never inject into a session a human is using. If
    the target is working or already owns an outstanding send prompt, the
    call fails with `busy`. To replace a direct child's current turn, call
    `interrupt_session`, wait until `list_participants` reports it idle,
    then retry the send. Only a direct parent may interrupt a participant.

    ``target_id`` may be a participant id or a name. Names work only
    while the participant is live; a dead participant's name is null and
    cannot be resolved. Because names are recyclable, use the id for any
    targeting that spans time or destructive consequences — a
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


async def interrupt_session(session: Session, *, target: str) -> dict:
    """Cancel one direct child's active turn and undelivered queued followups."""
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "participant.interrupt",
        target=target,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result


async def steer_session(
    session: Session, *, target: str, prompt: str, job_handle: str | None = None
) -> dict:
    """Amend exactly the current Theater job of one direct child.

    The prompt amends the child's active native turn in place: no new job
    handle is created, the job's original prompt and response-format contract
    are preserved, and the handle the caller already holds keeps awaiting the
    amended work. An optional ``job_handle`` asks the daemon to verify the
    amendment lands on exactly the job the caller expects; a mismatch is
    refused rather than amended.

    The daemon owns every decision: direct-parent authorization, native
    wiring, an active turn bound to a Theater job, and the capability check.
    A refusal is final — a steer is never retried and never reinterpreted as a
    send or a queue entry — so a ``stale_target`` refusal means the caller
    should queue a followup, not retry. The daemon's reply is returned verbatim,
    including any delivery-uncertainty it reports.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call(
        "participant.steer",
        target=target,
        prompt=prompt,
        job_handle=job_handle,
        caller_id=session.participant_id,
    )
    assert isinstance(record, dict)
    return record


async def queue_followup(
    session: Session, *, target: str, prompt: str, response_format: dict | None = None
) -> dict:
    """Queue a prompt for a child's next idle moment; return its handle now.

    The queue is Theater's own: the returned handle is an ordinary send job
    the caller can pass to ``await_sessions``, but it is not delivered until
    the target's current work ends and the daemon observes it idle. Ownership
    and policy are revalidated when the item actually dispatches — a followup
    that no longer qualifies finishes with an explicit error, and one whose
    native backend restarted before dispatch is never replayed into the new
    backend.

    ``response_format`` mirrors ``send``: a JSON Schema hint the daemon
    attaches to the job, forwarded unchanged.

    Authorization (direct parent or local operator), the pending bound, and
    delivery are all the daemon's to decide from the forwarded caller
    identity; the daemon's refusal and its reason are the answer.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call(
        "participant.queue_followup",
        target=target,
        prompt=prompt,
        response_format=response_format,
        caller_id=session.participant_id,
    )
    assert isinstance(record, dict)
    return record


async def update_session_settings(
    session: Session, *, target: str, model: str | None = None, reasoning_effort: str | None = None
) -> dict:
    """Change a direct child's model or reasoning effort while it is idle.

    Idle-only: the daemon refuses while the child has an active turn or a
    pending native interaction. The values must be on the existing
    model/reasoning allowlists — ``list_models`` reports them — and approval
    and sandbox policy are never changed here. Effective values are reported
    only after the native backend confirms; an uncertain delivery stays
    visibly uncertain in the daemon's reply, which is returned verbatim.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call(
        "participant.settings.update",
        target=target,
        model=model,
        reasoning_effort=reasoning_effort,
        caller_id=session.participant_id,
    )
    assert isinstance(record, dict)
    return record


async def get_session_controls(session: Session, *, target: str) -> dict:
    """Report one session's effective control state, answered by the daemon.

    Read-only: nothing is delivered or changed. The daemon returns the
    effective capabilities (send, steer, queue_followup, settings_update,
    interrupt) with its own reason for each unavailable one, the control
    connection's health, the effective settings, the active turn, and the
    handles of queued followups. No capability or availability judgement is
    made here — the daemon's reasons are the only ones worth reporting, so its
    reply is returned verbatim.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call(
        "participant.controls",
        target=target,
        caller_id=session.participant_id,
    )
    assert isinstance(record, dict)
    return record


async def scratchpad_write(
    session: Session, *, value: str, namespace: str, key: str | None = None
) -> dict:
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "scratchpad.write",
        namespace=namespace,
        value=value,
        key=key,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result


async def scratchpad_get(
    session: Session, *, namespace: str, keys: list[str] | None = None
) -> dict:
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "scratchpad.get",
        namespace=namespace,
        keys=keys,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result


async def put_child_back_in_the_wound(session: Session, *, target: str) -> dict:
    """Forward a direct-child kill to the daemon with the caller identity."""
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "participant.kill",
        id=target,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result
