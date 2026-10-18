"""Store rows and connection contexts built with the defaults most tests want."""

from __future__ import annotations

from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.schema import (
    bus,
    touch,
)
from theater.frontend.capabilities import ConnectionChannel, ConnectionRole
from theater.models import (
    Job,
    JobState,
    Participant,
    Status,
    Tier,
    now,
)


def make_participant(
    store,
    *,
    pid: str = "p1",
    harness: str = "vibe",
    cwd: str = "/tmp",
    parent_id: str | None = None,
    workspace_id: str | None = None,
    status: Status = Status.DEAD,
) -> Participant:
    p = Participant(
        id=pid,
        harness=harness,
        tier=Tier.SPAWNED,
        cwd=cwd,
        parent_id=parent_id,
        workspace_id=workspace_id,
        status=status,
    )
    store.upsert_participant(p)
    return p


def make_job(
    store,
    *,
    handle: str,
    caller_id: str = "cli",
    target_id: str | None = "p1",
    kind: str = "spawn",
    prompt: str | None = "do the thing",
    state: str = JobState.DONE,
    result: str | None = "done",
    error_code: str | None = None,
    created_at: float | None = None,
    finished_at: float | None = None,
) -> None:
    store.create_job(
        Job(
            handle=handle,
            caller_id=caller_id,
            target_id=target_id,
            kind=kind,
            prompt=prompt,
            state=state,
            result=result,
            error_code=error_code,
            created_at=created_at if created_at is not None else now(),
            finished_at=finished_at,
        )
    )


def running_send_job(handle: str) -> Job:
    return Job(
        handle=handle,
        caller_id="caller",
        target_id="target",
        kind="send",
        prompt="hi",
        state="running",
        result=None,
        error_code=None,
        created_at=1.0,
        finished_at=None,
    )


def operator_context() -> ConnectionContext:
    return ConnectionContext(
        client_id="operator-a",
        role=ConnectionRole.OPERATOR,
        channel=ConnectionChannel.RPC,
        api_major=1,
        api_minor=0,
        capabilities=frozenset({"orchestration.v1"}),
    )


def make_touch(
    store,
    *,
    job_handle: str,
    path: str = "src/main.py",
    mode: str = "write",
    sha_before: str | None = None,
    sha_after: str | None = None,
) -> None:
    store.conn.execute(
        touch.insert().values(
            job_handle=job_handle,
            path=path,
            mode=mode,
            sha_before=sha_before,
            sha_after=sha_after,
        )
    )


def make_bus_row(
    store,
    *,
    kind: str = "job.created",
    ts: float | None = None,
    from_id: str | None = None,
    to_id: str | None = None,
    payload: str | None = None,
) -> int:
    result = store.conn.execute(
        bus.insert().values(
            ts=ts if ts is not None else now(),
            from_id=from_id,
            to_id=to_id,
            kind=kind,
            payload=payload,
        )
    )
    pk = result.inserted_primary_key
    assert pk is not None
    return pk[0]


def alias_predecessor(registry, *, alias, canonical, session_id, live=False, pane=None):
    """Create a participant whose stored harness is an alias spelling.

    ``registry.register`` normalizes the harness at line 243, so calling it
    with ``harness="claude-code"`` stores ``"claude"`` and defeats the test.
    To get a genuinely alias-stored row we register under the canonical name,
    then mutate ``p.harness`` to the alias and re-upsert — bypassing the
    normalizer. Assert the stored spelling is the alias before returning.
    """
    p = registry.register(
        harness=canonical,
        pane=pane,
        cwd="/tmp",
        session_id=session_id,
    )
    p.harness = alias
    p.session_correlation = "exact"
    registry.store.upsert_participant(p)
    # Verify the alias spelling survived in the store.
    stored = registry.store.get_participant(p.id)
    assert stored.harness == alias, (
        f"expected alias {alias!r} in store, got {stored.harness!r}; "
        "registry.register normalized the harness and the test is hollow"
    )
    if not live:
        registry.mark_dead(p.id)
    return p
