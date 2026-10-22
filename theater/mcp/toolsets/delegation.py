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
    """Request a daemon-planned spawn and project its participant and operation receipt."""
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
    """Forward a prompt and project the daemon's job receipt."""
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
    """Forward settings changes and return the daemon's authoritative result."""
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
