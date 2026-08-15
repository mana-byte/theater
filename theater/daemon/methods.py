"""RPC method handlers for the daemon.

Each handler is registered via the @method decorator and dispatched by name
from the daemon's connection handler. The handlers are thin: they extract
parameters, call into the registry/jobs/observer, and return dicts.

Heavy logic (spawn, observe, rails) lives in dedicated modules; these
handlers just wire parameters to calls.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, NoReturn

from theater import protocol
from theater.daemon.harness_detect import detect_harness, is_shell
from theater.daemon.rails import (
    check_budget,
    check_cycle,
    check_depth,
    check_model_allowed,
    check_wait_cycle,
)
from theater.daemon.spawner import SpawnRequest
from theater.harness import HARNESSES, describe, normalize, supports_model
from theater.harness.observation import ScreenConfidence, ScreenKind
from theater.models import (
    AwaitingDecision,
    BadRequest,
    Busy,
    HumanPresent,
    JobState,
    NoSelfKill,
    NotAddressable,
    NotYourChild,
    StaleTarget,
    Status,
    now,
)
from theater.tmux import client as tmux
from theater.tmux.presence import human_present

if TYPE_CHECKING:  # circular at runtime: server imports this module for its handlers
    from theater.daemon.server import Daemon

logger = logging.getLogger(__name__)

Handler = Callable[["Daemon", dict[str, Any]], Awaitable[Any]]
METHODS: dict[str, Handler] = {}

#: Ceiling on a single `jobs.await`, whatever the caller asks for. An await
#: holds a daemon connection open and stretches the client's socket timeout to
#: match, so an agent that asks for an hour ties both up for an hour. Five
#: minutes is longer than any turn observed in practice, and a caller that
#: still wants more can simply await again — the job keeps running either way.
MAX_AWAIT = 300.0

#: How long a running send job keeps its exclusive claim on a pane. `send`
#: types the prompt into the pane and creates a job, but nothing verifies the
#: prompt reached the agent — a human at the pane can clear the composer (Esc),
#: and the prompt never enters the transcript. The job stays RUNNING, the
#: observer never sees a matching turn end, and the rescue timer cannot fire
#: on a participant that is actively working (every transcript event resets
#: the quiet clock). Past this TTL the job stops blocking the pane; it is not
#: finished, and the observer may still answer it, it has only lost its
#: reservation. Five minutes is long enough that a genuinely queued prompt
#: has had every chance to be read, and short enough that a lost one does not
#: wedge the participant for the rest of the session.
SEND_CLAIM_TTL = 300.0


def method(name: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        METHODS[name] = fn
        return fn

    return register


def _require(params: dict, key: str) -> Any:
    if key not in params or params[key] in (None, ""):
        raise BadRequest(f"missing required parameter {key!r}")
    return params[key]


@method("ping")
async def _ping(daemon, params: dict) -> dict:
    return {"pong": True, "protocol": protocol.PROTOCOL_VERSION}


@method("hello")
async def _hello(daemon, params: dict) -> dict:
    """First contact. Establishes or confirms the caller's identity and tier."""
    participant = daemon.registry.register(
        harness=params.get("harness") or "unknown",
        pane=params.get("pane"),
        cwd=params.get("cwd"),
        session_id=params.get("session_id"),
        claimed_id=params.get("id"),
    )
    return participant.to_dict()


@method("participants.list")
async def _list(daemon, params: dict) -> list[dict]:
    include_dead = bool(params.get("include_dead"))
    return [p.to_dict() for p in daemon.registry.list(include_dead=include_dead)]


@method("participants.tree")
async def _tree(daemon, params: dict) -> list[dict]:
    return daemon.registry.tree()


@method("participants.get")
async def _get(daemon, params: dict) -> dict:
    return daemon.registry.get(_require(params, "id")).to_dict()


@method("participant.status")
async def _status(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    raw = _require(params, "status")
    try:
        status = Status(raw)
    except ValueError:
        raise BadRequest(f"unknown status {raw!r}") from None
    daemon.registry.set_status(pid, status)
    return daemon.registry.get(pid).to_dict()


@method("participant.kill")
async def _kill(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    caller_id = params.get("caller_id") or "cli"

    target = daemon.registry.get(pid)

    if caller_id != "cli":
        if target.id == caller_id:
            raise NoSelfKill(
                f"refusing to kill {pid!r}: that is you, not your child"
            )
        if target.parent_id != caller_id:
            raise NotYourChild(
                f"refusing to kill {pid!r}: its parent is "
                f"{target.parent_id!r}, not you ({caller_id!r})"
            )
        if target.status is Status.DEAD:
            return {"id": pid, "killed": False, "reason": "already_dead"}

    await daemon.spawner.kill(pid)
    return {"id": pid, "killed": True}


@method("adopt")
async def _adopt(daemon, params: dict) -> dict:
    """Adopt a pane the user is already running a harness in."""
    pane = _require(params, "pane")
    override = params.get("harness")
    cwd = params.get("cwd")
    if not tmux.available():
        raise BadRequest("tmux is not available; cannot look up pane")
    panes = await tmux.list_panes()
    match = next((p for p in panes if p.pane_id == pane), None)
    if match is None:
        raise BadRequest(f"pane {pane!r} not found in tmux")
    harness = normalize(override) if override else detect_harness(
        match.current_command, match.pane_pid
    )
    if cwd is None:
        cwd = match.cwd
    participant = daemon.registry.register(
        harness=harness,
        pane=pane,
        cwd=cwd,
    )
    # Record the launch epoch from the row we already looked up. For an
    # adopted pane this is the shell tmux forked, not the harness — the
    # harness is its descendant — so it stays constant when the CLI exits.
    # That is why the delivery gate cannot rely on the pid alone.
    participant = daemon.registry.attach_pane(
        participant.id, pane, pane_pid=match.pane_pid
    )
    return participant.to_dict()


@method("participants.unmanaged")
async def _unmanaged(daemon, params: dict) -> list[dict]:
    """Panes running a known harness binary with no participant record."""
    if not tmux.available():
        return []
    panes = await tmux.list_panes()
    registered = {p.tmux_pane for p in daemon.registry.list() if p.tmux_pane}
    out: list[dict] = []
    for p in panes:
        if p.pane_id in registered:
            continue
        harness = detect_harness(p.current_command, p.pane_pid)
        if harness != "unknown":
            out.append(
                {
                    "pane": p.pane_id,
                    "command": p.current_command,
                    "harness": harness,
                    "cwd": p.cwd,
                    "session": p.session,
                    "window_name": p.window_name,
                }
            )
    return out


@method("spawn")
async def _spawn(daemon, params: dict) -> dict:
    req = SpawnRequest(
        harness=_require(params, "harness"),
        prompt=params.get("prompt") or "",
        cwd=_require(params, "cwd"),
        approval=_require(params, "approval"),
        parent_id=params.get("parent_id"),
        tmux_session=params.get("tmux_session"),
        window_name=params.get("window_name"),
        background=params.get("background", True),
        worktree=bool(params.get("worktree", False)),
        base_branch=params.get("base_branch"),
        model=params.get("model"),
        resume=params.get("resume"),
    )
    # Safety rails: reject before creating anything.
    rails = daemon.config.rails
    check_depth(daemon.store, req.parent_id, cap=rails.depth_cap)
    check_budget(daemon.store, req.parent_id, limit=rails.budget)
    # Policy, not capability: `Spawner` asks the adapter whether it can take a
    # model at all, which it can answer alone. Whether the user permits *this*
    # model is a question only the config can answer, and the spawner has none.
    check_model_allowed(
        req.harness, req.model, daemon.config.models_for(req.harness)
    )

    participant = await daemon.spawner.spawn(req)
    # Create a job for this spawn so the caller can await the result.
    handle = participant.id  # the handle is the participant id itself.
    daemon.jobs.create(
        handle=handle,
        caller_id=params.get("parent_id") or "cli",
        target_id=participant.id,
        kind="spawn",
        prompt=req.prompt or "",
        # participant.cwd is the directory the child actually runs in: the
        # spawner sets it to the worktree path when worktree=True (spawner.py:93),
        # or leaves it as the requested cwd otherwise. Hashing against the
        # parent repo when the child was in a worktree would resolve paths to
        # the wrong files.
        cwd=participant.cwd,
    )
    if not req.prompt:
        # Nothing was asked, so there is nothing to wait for: the job is done
        # the moment the pane exists. `theater spawn vibe` and the régie
        # palette both start a bare CLI this way.
        #
        # Left running, this job never resolves on its own. It then eats the
        # first turn end the human produces — which belongs to no caller — and
        # counts as work in flight, so every `send` to that participant is
        # refused as busy. Resolving it here keeps both of those honest, and
        # `await_sessions` on the handle still works: it returns immediately.
        daemon.jobs.finish(handle, state=JobState.DONE, result="")
    result = participant.to_dict()
    result["handle"] = handle
    return result


@method("jobs.await")
async def _jobs_await(daemon, params: dict) -> list[dict]:
    """Wait for one or more jobs to finish, up to max_wait seconds.

    A handle nobody knows is an error, not an empty list. `await_jobs`
    silently skips what it cannot find, so a typo or a handle from a previous
    daemon used to come back as `[]` — indistinguishable from "nothing to
    report", which sent agents into retry loops against a job that never
    existed.

    The rails run before that complaint. A caller aiming at the wrong end of
    a loop should be told so, whether or not the thing it named turned out to
    be awaitable; "you would deadlock" is the more useful of the two answers.
    """
    handles = params.get("handles") or []
    if not handles:
        raise BadRequest("at least one handle is required")
    max_wait = min(max(float(params.get("max_wait", 150.0)), 0.0), MAX_AWAIT)
    caller_id = params.get("caller_id")

    known = {h: daemon.jobs.get(h) for h in handles}
    # Cycles are about participants, and a send handle is `<target>#<n>`
    # rather than a participant id — so resolve through the jobs. A handle
    # with no job behind it can still name a participant (a spawn handle is
    # its own participant id), and that is worth checking even though the
    # await itself is about to be refused.
    targets = []
    for handle, job in known.items():
        if job is not None:
            if job.target_id:
                targets.append(job.target_id)
        elif daemon.store.get_participant(handle) is not None:
            targets.append(handle)
    if caller_id:
        check_cycle(daemon.store, caller_id, targets)
        check_wait_cycle(daemon.jobs.wait_graph, caller_id, targets)

    missing = [h for h, job in known.items() if job is None]
    if missing:
        raise BadRequest(f"no such job(s): {', '.join(sorted(missing))}")

    with daemon.jobs.waiting(caller_id, targets):
        jobs = await daemon.jobs.await_jobs(handles, max_wait=max_wait)
    return [j.to_dict() for j in jobs]


@method("jobs.status")
async def _jobs_status(daemon, params: dict) -> dict:
    """Get the current state of a single job."""
    handle = _require(params, "handle")
    job = daemon.jobs.get(handle)
    if job is None:
        raise BadRequest(f"no job {handle!r}")
    return job.to_dict()


@method("bus.tail")
async def _bus_tail(daemon, params: dict) -> list[dict]:
    return daemon.store.bus_tail(
        limit=int(params.get("limit", 100)), after_id=int(params.get("after_id", 0))
    )


@method("stats")
async def _stats(daemon, params: dict) -> dict:
    """How turns have been ending, per harness.

    Read straight out of SQLite on each call rather than kept as live counters:
    the numbers are only interesting over hours, a restart must not reset them,
    and a counter that exists solely to be printed is a thing to keep in sync
    for nothing.

    `window` is in hours and cuts on job creation time; omit it for all of
    history. Cutting on creation rather than completion so a turn that is still
    running counts in the window it was asked in.
    """
    window = params.get("window")
    since = None if window in (None, "") else now() - float(window) * 3600.0
    return {
        "since": since,
        "harnesses": daemon.store.turn_outcomes(since=since),
        "refusals": daemon.store.refusal_counts(since=since),
    }


def _refuse_send(
    daemon, exc: Exception, *, reason: str, caller_id: str, target_id: str
) -> NoReturn:
    """Record a send that never became a job, then raise it.

    These refusals happen before anything is reserved, so there is no job row
    to carry them and — until this — no trace anywhere: a user watching
    `theater bus` saw a send simply not happen. The caller does get an error
    back, but the caller is usually an agent, which reports it in its own words
    or quietly moves on.

    Counted by `Store.refusal_counts`. Kept as one bus kind with a `reason`
    rather than one kind per refusal, so a reader can subscribe to all of them
    without knowing the list, and so adding a reason does not need a
    subscriber change.
    """
    daemon.store.bus_append(
        "send.refused",
        from_id=caller_id,
        to_id=target_id,
        payload={"reason": reason, "detail": str(exc)},
    )
    raise exc


async def _check_pane_identity(
    daemon, target, refuse: Callable[..., NoReturn]
) -> None:
    """Refuse to type into a pane that is no longer this participant's.

    The failure this exists for is the only irreversible one Theater has. A
    CLI exits, its pane falls back to a shell, and the next paste plus Enter
    runs the prompt as a shell command — which is how an agent came to be
    answered by `(eval):1: not enough directory stack entries`. Every other
    delivery bug produces a wrong answer that can be retried.

    Three checks, in descending order of how much the evidence is worth:

      1. **The pane exists.** A fact from tmux. Absent, the participant is
         gone: mark it dead, which is what the reconcile sweep would conclude
         at its next pass anyway.
      2. **The pid still matches the launch epoch.** Also a fact from tmux.
         tmux never recycles pane ids, but `respawn-pane` keeps the id and
         replaces the process behind it, so equality of pane id is not
         equality of occupant. Skipped when no epoch was recorded.
      3. **A harness is still in the process tree.** Weaker: it walks `ps`,
         which can fail, and a failure is indistinguishable from an exit. So
         "no harness found" alone is not enough to refuse — the pane's
         foreground must *also* be a shell, which is a fact from tmux and is
         precisely the dead-CLI shape. An agent running its bash tool trips
         the shell half and not the tree half, so it passes.

    Marking dead is reserved for 1 and 2, where tmux is the witness. Case 3
    refuses without destroying the record: `ps` was the only witness, and if
    it lied, a demotion to dead would need a human to undo. The registry
    already marks the old occupant dead when a new one claims the same pane.

    Fails open when tmux itself errors. A gate that turns an unrelated tmux
    hiccup into an unreachable participant reproduces the harm it exists to
    prevent, and it costs nothing to skip: the delivery immediately after
    goes through the same tmux and fails cleanly on its own.
    """
    if not tmux.available():
        return
    try:
        pane = await tmux.pane_info(target.tmux_pane)
    except Exception as exc:  # pragma: no cover - tmux failing mid-send
        logger.warning("pane identity check failed for %s: %s", target.id, exc)
        return

    if pane is None:
        daemon.registry.mark_dead(target.id)
        refuse(
            StaleTarget(
                f"pane {target.tmux_pane} of {target.id!r} no longer exists"
            ),
            reason="pane_gone",
        )

    if target.pid is not None and pane.pane_pid != target.pid:
        daemon.registry.mark_dead(target.id)
        refuse(
            StaleTarget(
                f"pane {target.tmux_pane} was respawned "
                f"(pid {target.pid} -> {pane.pane_pid}); {target.id!r} is gone"
            ),
            reason="pane_replaced",
        )

    if target.harness == "unknown":
        # Nothing to compare against. Adopted participants whose harness could
        # not be identified are addressable today and work; refusing them on
        # the strength of a second failed identification would be a
        # regression, not a fix.
        return

    found = detect_harness(pane.current_command, pane.pane_pid)
    if found == target.harness:
        return
    if found != "unknown":
        refuse(
            StaleTarget(
                f"pane {target.tmux_pane} is running {found!r}, "
                f"not {target.harness!r}; {target.id!r} has lost its seat"
            ),
            reason="harness_changed",
        )
    if is_shell(pane.current_command):
        refuse(
            StaleTarget(
                f"{target.harness} has exited in pane {target.tmux_pane}; "
                f"a shell ({pane.current_command}) is at the prompt"
            ),
            reason="harness_gone",
        )


async def _check_approval_modal(
    daemon, target, refuse: Callable[..., NoReturn]
) -> None:
    """Refuse to type into a pane showing an approval or trust modal.

    At an approval prompt, Enter is a button press, so an injected prompt can
    auto-approve a tool call the human never saw — the one false positive with
    an unrecoverable cost (`docs/v1.6_observation.md` lines 88-91). This gate
    captures a fresh screen reading and refuses only when the kind is
    `APPROVAL` or `TRUST` at `HIGH` confidence. Everything else — `UNKNOWN`,
    `WORKING`, `PROMPT`, and low-confidence `APPROVAL` — lets the send
    through.

    Why a fresh capture, not the stored `Status`: v1.7 planned this exact gate
    ("C3, the modal veto") and dropped it for two reasons
    (`docs/v1.7_hardening.md` lines 223-228). (a) `is_idle_screen` is a boolean
    and cannot separate "waiting at its input prompt" from "showing an approval
    modal", so a veto would block ordinary idle panes. (b) It explicitly rejects
    gating on `AWAITING_INPUT`, a display hint tuned to accept false negatives,
    because using it as a control signal would make a stuck `WORKING` pane
    unreachable. Both are answered now: (a) by `ScreenReading`, which carries
    a `kind` plus a `confidence`; (b) by reading a fresh capture, not the
    stored status. A stuck pane reads `working` or `unknown`, never
    `approval`, so it stays reachable. Reading the status instead would also be
    stale by up to a poll interval.

    The `high` requirement is the safety margin. A false refusal makes a
    healthy pane permanently unreachable, so only a marker verified against a
    real captured screen may block a caller. The default `screen_reading` shim
    never returns `approval` at all — it maps to `prompt`/`unknown`, both at
    `low` — so a third-party plugin that only implements the old boolean can
    never accidentally brick a pane.

    Fails open, like `_check_pane_identity`: if the capture or the harness
    lookup raises, log it and let the send through. A gate that turns a
    transient tmux error into an unreachable pane is worse than the risk it
    removes (`docs/v1.7_hardening.md` lines 234-238).
    """
    harness = HARNESSES.get(normalize(target.harness))
    if harness is None:
        return

    try:
        capture = await tmux.run(
            "capture-pane", "-p", "-t", target.tmux_pane, check=False
        )
    except Exception as exc:  # pragma: no cover - tmux failing mid-send
        logger.warning("approval-modal capture failed for %s: %s", target.id, exc)
        return

    try:
        reading = harness.observer.screen_reading(capture)
    except Exception as exc:  # pragma: no cover - third-party observer
        logger.warning("screen_reading failed for %s: %s", target.id, exc)
        return

    if (
        reading.kind in (ScreenKind.APPROVAL, ScreenKind.TRUST)
        and reading.confidence == ScreenConfidence.HIGH
    ):
        refuse(
            AwaitingDecision(
                f"pane {target.tmux_pane} of {target.id!r} is showing an "
                f"approval modal ({reading.kind}); not injecting"
            ),
            reason="awaiting_decision",
        )


@method("send")
async def _send(daemon, params: dict) -> dict:
    """Send a prompt to an already-running agent by pasting into its pane."""
    target_id = _require(params, "target")
    prompt = _require(params, "prompt")
    caller_id = params.get("caller_id") or "cli"

    refuse = functools.partial(
        _refuse_send, daemon, caller_id=caller_id, target_id=target_id
    )

    target = daemon.registry.get(target_id)
    if not target.addressable:
        refuse(
            NotAddressable(
                f"participant {target_id!r} is not addressable (tier={target.tier})"
            ),
            reason="not_addressable",
        )
    if not target.tmux_pane:
        refuse(
            NotAddressable(f"participant {target_id!r} has no pane to send to"),
            reason="no_pane",
        )

    # Before asking whether a human is at the pane, establish that the pane
    # is still the participant's at all.
    await _check_pane_identity(daemon, target, refuse)

    if await human_present(target.tmux_pane):
        refuse(
            HumanPresent(f"a human is present at {target.tmux_pane}; not injecting"),
            reason="human_present",
        )

    # A human is not at the pane; check whether the pane itself is waiting on
    # a human decision. This costs a capture-pane, so it runs after the
    # cheaper presence check.
    await _check_approval_modal(daemon, target, refuse)

    # Busy is "someone is already waiting on a turn from this participant",
    # which is any running job that carried a prompt — a spawn prompt occupies
    # the pane exactly as much as a send does. A promptless job is not evidence
    # of work; `_spawn` resolves those on the spot, so in practice there are
    # none, and the filter is here so a stray one cannot wedge a participant.
    #
    # Status is deliberately not consulted. It is inferred from a transcript
    # and has been wrong before; a stuck WORKING would silently make a
    # participant unreachable, and that failure is worse than a prompt landing
    # while a human's turn is still running.
    #
    # The same argument applies to a stuck job. A running job is an
    # unverifiable claim that a prompt sits in the pane's queue — `send`
    # typed it, but nothing confirms the agent received it, and a human at the
    # pane can clear the composer before it is read. A job that has held its
    # reservation past `SEND_CLAIM_TTL` is dropped from this check: it is not
    # finished, and the observer may still answer it if a matching turn end
    # arrives, but it no longer blocks the pane. This is the exception the
    # paragraph above already makes for a stuck status, extended to the case
    # it missed.
    stale = now() - SEND_CLAIM_TTL
    if [
        j
        for j in daemon.store.running_jobs_for_target(target_id)
        if j.prompt and j.created_at > stale
    ]:
        refuse(
            Busy(f"participant {target_id!r} has a running send job"),
            reason="busy",
        )

    # Reserve the job before typing, in that order for two reasons. The check
    # above and this create must not be separated by an await, or two sends
    # racing through the daemon both read an empty queue and both type into the
    # pane. And a fast agent can finish its turn before the RPC returns: with
    # the job created afterwards, the observer would see the turn end with
    # nothing running and the caller would await a promise already broken.
    handle = f"{target_id}#{daemon._next_send_seq()}"
    daemon.jobs.create(
        handle=handle,
        caller_id=caller_id,
        target_id=target_id,
        kind="send",
        prompt=prompt,
        # The target's cwd, not the caller's: the target is the one whose
        # tool calls touch files, and its cwd is where those paths resolve.
        cwd=target.cwd,
    )
    try:
        await tmux.deliver_text(target.tmux_pane, prompt)
    except Exception as exc:
        # Nothing was delivered, so nothing will ever answer. Close the job
        # rather than leave a reservation that blocks the next send until the
        # rescue timer clears it.
        daemon.jobs.finish(
            handle,
            state=JobState.CRASHED,
            result=str(exc),
            error_code="send_failed",
        )
        raise

    daemon.store.bus_append(
        "agent.send",
        from_id=caller_id,
        to_id=target_id,
        payload={"handle": handle, "prompt": prompt[:200]},
    )

    result = daemon.jobs.get(handle)
    assert result is not None
    return result.to_dict()


@method("harnesses")
async def _harnesses(daemon, params: dict) -> list[dict]:
    """What this daemon can actually spawn.

    The registry is importable by anyone, so this looks redundant — but the
    daemon reads its config once at start and never reloads, so a config edit
    leaves the CLI and the régie holding a *newer* harness set than the process
    that has to honour it. Offering a spawn the daemon then refuses is the
    failure this method exists to prevent, and it becomes real the moment the
    set stops being a hardcoded literal.
    """
    return describe()


@method("models")
async def _models(daemon, params: dict) -> list[dict]:
    """The model allowlist this daemon will actually enforce, per harness.

    Exists for the same reason as `harnesses`, one level down: the allowlist is
    read out of `daemon.config` at start-up and never reloaded, so after an edit
    the file on disk and the process that refuses the spawn disagree. `theater
    models` reports the file, which is right for a human about to edit it; a
    caller asking "what will be accepted" has to be told what this daemon holds.

    Every registered harness gets a row, including one with no entry: absent is
    the common case and the informative one, since that harness refuses every
    named model. Rows carrying an `error`, or a binary that is not installed,
    are reported too — `describe` already decided they are worth naming, and a
    consumer that only wants spawnable ones filters on the same two fields it
    filters `harnesses` on.

    `supported` and `models` are two different gates, and an empty list alone
    cannot say which one would stop a spawn: `supported` is the adapter's
    capability (`check_model`), `models` is the user's policy
    (`check_model_allowed`). Supported with an empty list is one config edit
    away from working; unsupported cannot take a model however the config reads.
    """
    rows = []
    for row in describe():
        name = row["name"]
        harness = HARNESSES.get(name)
        rows.append(
            {
                "harness": name,
                "models": daemon.config.models_for(name),
                "supported": harness is not None and supports_model(harness),
                "installed": row["installed"],
                "error": row["error"],
            }
        )
    return rows


@method("shutdown")
async def _shutdown(daemon, params: dict) -> dict:
    daemon.stop()
    return {"stopping": True}


#: Kinds `read_transcript` reports. ERROR is dropped: the caller is an agent
#: reading what was said, and a harness-level error record is not part of the
#: conversation.
_READABLE = ("assistant", "user", "tool_call", "tool_result")


@method("read_transcript")
async def _read_transcript(daemon, params: dict) -> dict:
    """Read a participant's session back, with the text unclipped.

    Goes through the observer's `Source`, not through `find_transcript`, so an
    adapter whose output is a database answers this as well as one that writes
    a file. The source opened here is short-lived and separate from the
    watcher's: reading history must not move the watcher's cursor.
    """
    pid = _require(params, "id")
    last_n = int(params.get("last_n", 5))

    p = daemon.registry.get(pid)
    if p is None:
        raise BadRequest(f"no participant {pid!r}")

    harness_name = normalize(p.harness)
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise BadRequest(f"cannot read transcript: harness {p.harness!r} is not known")

    source = harness.observer.open_source(
        cwd=p.cwd, session_id=p.session_id, after=None
    )
    try:
        history = await source.history(last_n=last_n)
    finally:
        await source.aclose()

    events = [
        {
            "index": event.raw_index,
            "role": str(event.kind),
            "text": event.text or "",
            "tool_name": event.tool_name,
            "turn_end": event.turn_end,
        }
        for event in history.events
        if event.kind.value in _READABLE
    ]
    return {"id": pid, "events": events, "path": history.location}
