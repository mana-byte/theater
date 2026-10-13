"""Spawning, sending (and redirecting a busy child), settings, adoption, and killing.

These are the tools an agent uses to delegate work to other agents and
coordinate with them. ``_summarise`` is imported from the participants toolset
to project the returned participant record.

The runtime-control bodies below are thin forwarders: the daemon owns every
policy decision (authorization, capability, idle, allowlists) and every
refusal reason, and each call names the calling participant so the daemon can
make those decisions on the real identity.
"""

from __future__ import annotations

from pathlib import Path

from theater.constants.daemon import RPC_DEFAULT_MAX_WAIT_SECONDS
from theater.mcp.session import Session
from theater.mcp.toolsets.participants import _summarise
from theater.mcp.toolsets.projection import compact, job_entry


async def harnesses(
    session: Session, harness: str | None = None, include_models: bool = False
) -> list[dict]:
    """Report spawnable harnesses using the daemon's active approvals and optional allowlists."""
    rows = await session.client.call("harnesses")
    assert isinstance(rows, list)
    available = [
        row
        for row in rows
        if row["installed"] and not row["error"] and (harness is None or row["name"] == harness)
    ]
    model_rows = {}
    if include_models:
        models = await session.client.call("models")
        assert isinstance(models, list)
        model_rows = {
            row["harness"]: row for row in models if row["installed"] and not row["error"]
        }
    result = []
    for row in available:
        item = {"name": row["name"], "approvals": row.get("approvals")}
        if row["binary"] != row["name"]:
            item["binary"] = row["binary"]
        if include_models:
            model = model_rows.get(row["name"])
            if model is None:
                continue
            item.update(
                models=model["models"],
                supported=model["supported"],
                reasoning=model.get("reasoning", []),
                reasoning_supported=model.get("reasoning_supported", False),
            )
        result.append(item)
    return result


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
    provider: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    resume: str | None = None,
    name: str | None = None,
    description: str | None = None,
    wiring: str = "auto",
    after: str | None = None,
    task_kind: str | None = None,
    skills: list[str] | None = None,
) -> dict:
    """Create a child agent through a selected terminal provider and return its record.

    The prompt is delivered on the child's argv by the selected provider, so
    this path does not depend on keystroke injection working at all.

    If `worktree` is True, a git worktree is created for the child so it has
    its own isolated index and HEAD. Its unique branch name is
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
    select a model at all. Both are refused before anything is created;
    `list_harnesses(include_models=true)` reports them. Membership is the whole
    of the check — Theater cannot confirm
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

    `response_format` is forwarded unchanged; MCP neither serializes nor
    injects it. A bare JSON Schema object is a hint the daemon adds to the
    prompt guidance exactly once and later parses with ``json.loads``: no
    validation, scraping, coercion, or retry. A ``{"$theater": ...}`` object
    is a strict contract (presets ``review``, ``debate``, ``delegate``, or
    ``custom`` with a name and schema) the daemon validates in full, repairs
    where it can prove which turn answered, and otherwise ends with
    ``structured_status`` ``unable_to_parse``. Resume plus `response_format`
    is refused for harnesses whose resume path cannot carry a prompt.

    `skills` names are forwarded unchanged; the daemon resolves, renders, and
    bounds them. Omitted or empty means none: nothing is inherited.

    The returned ``session_id`` is populated asynchronously by the observer,
    so the compact MCP projection normally omits it for a newly spawned child.
    Re-list later after Theater attaches to the child's transcript.

    ``wiring`` selects a preference: "auto" (default) and "native" use a
    compatible runtime when available, otherwise retain the ordinary launch;
    "legacy" opts out. It is independent of ``approval``, which remains
    required with no default.
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
        worktree=worktree,
        base_branch=base_branch,
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
        resume=resume,
        name=name,
        description=description,
        response_format=response_format,
        wiring=wiring,
        after=after,
        task_kind=task_kind,
        skills=skills,
    )
    assert isinstance(record, dict)
    return {
        **_summarise(record),
        **compact(
            {
                key: record.get(key)
                for key in (
                    "handle",
                    "operation_id",
                    "operation_state",
                    "operation_timed_out",
                    "operation_error",
                    "operation_warning",
                    "handoff",
                )
            }
        ),
    }


async def await_sessions(
    session: Session,
    *,
    handles: list[str],
    max_wait: float = RPC_DEFAULT_MAX_WAIT_SECONDS,
    ceiling: float | None = None,
) -> list[dict]:
    """Forward presence-aware awaits, projecting each entry to what the caller acts on."""
    if not session._resolved:
        await session.identify()
    if ceiling is not None:
        # Past the client's own threshold the call is backgrounded and its result lost.
        max_wait = min(max_wait, ceiling)
    # Caller identity lets the daemon reject mutual-await deadlocks.
    jobs = await session.client.call(
        "jobs.await",
        handles=handles,
        max_wait=max_wait,
        caller_id=session.participant_id,
    )
    assert isinstance(jobs, list)
    return [job_entry(job) for job in jobs]


async def send_prompt(
    session: Session,
    *,
    target_id: str,
    prompt: str,
    response_format: dict | None = None,
    if_busy: str = "auto",
    job_handle: str | None = None,
    after: str | None = None,
    skills: list[str] | None = None,
) -> dict:
    """Send a prompt to an already-running agent on its selected transport.

    Delivery follows the daemon's selected capability route. A native attempt
    with an unknown or disconnected outcome is never silently retried into
    the pane. The target must have a verified physical delivery route; origin
    and ownership do not decide ordinary send admission. By default the daemon
    delivers once when it is safe and otherwise queues the message, behind the
    target's work or a human at its pane, under the same handle. Explicit
    ``refuse`` is open to anyone; ``queue``, ``amend`` and ``replace`` stay
    direct-parent-only.

    ``target_id`` may be a participant id or a name. Names work only
    while the participant is live; a dead participant's name is null and
    cannot be resolved. Because names are recyclable, use the id for any
    targeting that spans time or destructive consequences — a
    recycled name can identify a successor.

    `response_format` is forwarded unchanged and takes the same forms as for
    ``spawn_session``: a bare schema is a prompt-only hint parsed with
    ``json.loads``; a ``{"$theater": ...}`` object is a strict contract the
    daemon validates in full and repairs where it can prove the turn.

    `after` (a name or exact job handle) queues the message until that work ends and
    adds Theater's labelled card about it; the daemon resolves and gates it.

    `skills` names are forwarded unchanged and attach to this message only.

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
        if_busy=if_busy,
        job_handle=job_handle,
        after=after,
        skills=skills,
    )
    assert isinstance(record, dict)
    return job_entry(record)


async def cancel_job(session: Session, *, handle: str) -> dict:
    """Withdraw work the requester made that was provably never started."""
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "jobs.cancel", handle=handle, caller_id=session.participant_id
    )
    assert isinstance(result, dict)
    return result


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


async def update_session_settings(
    session: Session, *, target: str, model: str | None = None, reasoning_effort: str | None = None
) -> dict:
    """Change a direct child's model or reasoning effort while it is idle.

    Idle-only: the daemon refuses while the child has an active turn or a
    pending native interaction. The values must be on the existing
    model/reasoning allowlists — ``list_harnesses(include_models=true)`` reports them — and approval
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
    """Compact diagnostics without changing the daemon's availability decisions."""
    if not session._resolved:
        await session.identify()
    record = await session.client.call(
        "participant.controls",
        target=target,
        caller_id=session.participant_id,
    )
    assert isinstance(record, dict)
    record = dict(record)
    record.pop("backend_generation", None)
    identity = record.get("transcript_identity")
    if isinstance(identity, dict) and record.get("native_session_id") == identity.get("session_id"):
        record.pop("native_session_id", None)
    presence = record.get("human_presence")
    if isinstance(presence, dict):
        record["human_presence"] = {
            key: value for key, value in presence.items() if key not in ("revision", "observed_at")
        }
    capabilities = record.get("capabilities")
    if isinstance(capabilities, dict):
        capabilities = {
            key: dict(value) if isinstance(value, dict) else value
            for key, value in capabilities.items()
        }
        record["capabilities"] = capabilities
        entries = [entry for entry in capabilities.values() if isinstance(entry, dict)]
        route = {}
        for field in ("transport", "runtime_host"):
            values = [entry.get(field) for entry in entries]
            # Missing routes must not inherit another capability's native host or transport.
            if not values or not all(values):
                continue
            shared = max(values, key=values.count)
            if values.count(shared) < 2:
                continue
            route[field] = shared
            for entry in entries:
                if entry.get(field) == shared:
                    del entry[field]
        if route:
            record["capability_route"] = route

    def without_empty(value):
        if isinstance(value, dict):
            return {
                key: kept
                for key, item in value.items()
                if (kept := without_empty(item)) not in (None, "", [], {})
            }
        if isinstance(value, list):
            return [without_empty(item) for item in value]
        return value

    return without_empty(record)


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
    session: Session,
    *,
    namespace: str,
    keys: list[str] | None = None,
    after_key: str | None = None,
    keys_only: bool | None = None,
) -> dict:
    if not session._resolved:
        await session.identify()
    metadata = keys is None if keys_only is None else keys_only
    result = await session.client.call(
        "scratchpad.get",
        namespace=namespace,
        keys=keys,
        after_key=after_key,
        keys_only=metadata,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    # Plugin sidecars read the daemon reply as is; only the agent's copy drops what it echoed.
    result = {k: v for k, v in result.items() if k != "after_key" or v is not None}
    if not metadata:
        result.pop("namespace", None)
        result.pop("keys", None)
    return result


async def scratchpad_delete(
    session: Session,
    *,
    namespace: str,
    keys: list[str] | None = None,
    digests: list[str] | None = None,
    clear: bool = False,
) -> dict:
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "scratchpad.delete",
        namespace=namespace,
        keys=keys,
        digests=digests,
        clear=clear,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result


async def terminate_session(session: Session, *, target: str, forget: bool = False) -> dict:
    """Forward a direct-child kill to the daemon with the caller identity."""
    if not session._resolved:
        await session.identify()
    params = {"forget": True} if forget else {}
    result = await session.client.call(
        "participant.kill",
        id=target,
        caller_id=session.participant_id,
        **params,
    )
    assert isinstance(result, dict)
    return result


async def adopt_session(session: Session, *, target: str) -> dict:
    """Forward a sibling adoption to the daemon with the caller identity."""
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "participant.adopt",
        id=target,
        caller_id=session.participant_id,
    )
    assert isinstance(result, dict)
    return result
