"""RPC method handlers for the daemon.

Each handler is registered via the @method decorator and dispatched by name
from the daemon's connection handler. The handlers are thin: they extract
parameters, call into the registry/jobs/observer, and return dicts.

Heavy logic (spawn, observe, rails) lives in dedicated modules; these
handlers just wire parameters to calls.
"""

from __future__ import annotations

import asyncio
import functools
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from sqlalchemy import func, select

from theater import paths, proc, protocol
from theater.daemon import lineage, workers, worktree
from theater.daemon.harness_detect import (
    PaneHarnessVerdict,
    compare_detected_harness,
    detect_harness,
    match_binary,
)
from theater.daemon.rails import (
    check_budget,
    check_cycle,
    check_depth,
    check_model_allowed,
    check_reasoning_allowed,
    check_wait_cycle,
)
from theater.daemon.schema import bus, jobs
from theater.daemon.spawner import SpawnRequest
from theater.harness import (
    HARNESSES,
    describe,
    normalize,
    supports_model,
    supports_reasoning,
    supports_resume,
)
from theater.harness.observation import (
    ScreenConfidence,
    ScreenKind,
    enumerate_transcript_candidates,
    open_participant_source,
)
from theater.harness.source import TranscriptCandidate
from theater.models import (
    AwaitingDecision,
    BadRequest,
    Busy,
    HumanPresent,
    Job,
    JobState,
    NoSelfKill,
    NotAddressable,
    NotYourChild,
    Participant,
    StaleTarget,
    Status,
    Tier,
    TranscriptIdentityLost,
    TranscriptUntrusted,
    new_id,
    now,
)
from theater.provenance import (
    TranscriptProvenance,
    is_trusted_provenance,
    normalize_provenance,
    provenance_at_least,
)
from theater.tmux import client as tmux
from theater.tmux.presence import human_present
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
    canonical_location,
    same_location,
    transcript_identity_recovery_message,
)

if TYPE_CHECKING:
    from theater.daemon.server import Daemon

logger = logging.getLogger(__name__)

Handler = Callable[["Daemon", dict[str, Any]], Awaitable[Any]]
METHODS: dict[str, Handler] = {}

#: Ceiling on a single `jobs.await`. An await holds a connection open and
#: stretches the client's socket timeout to match. Five minutes is longer
#: than any turn observed; a caller wanting more can await again.
MAX_AWAIT = 300.0

#: How long an await must stay blocked before it is announced on the bus.
#:
#: `job.await.start` exists so the régie can draw the line between an agent and
#: whatever it is stuck on. An agent polling `await_sessions(handles,
#: max_wait=0.1)` in a loop is not stuck on anything, yet announcing at call
#: entry writes two rows per handle per call — six handles polled ten times a
#: second is 120 rows/second of churn. That is not merely noise: `bus_tail`
#: returns the *newest* rows up to its limit and silently drops the rest, so the
#: churn can push some *other* await's `job.await.end` out of the régie's next
#: read and leave that animation running forever. Waiting this long first makes
#: the row mean "this agent is really waiting" rather than "this agent called
#: await". A quarter second is longer than any await that was never going to
#: block, and short enough that a real wait is on screen before anyone looks.
#: Read at call time, so a test can patch it rather than sleep.
AWAIT_ANNOUNCE_AFTER = 0.25

#: How long a running send job keeps its exclusive claim on a pane. Nothing
#: verifies the prompt reached the agent — a human can clear the composer
#: before it is read, leaving the job RUNNING with no matching turn end. Past
#: this TTL the job stops blocking the pane; the observer may still answer
#: it if a turn end arrives, it has only lost its reservation.
SEND_CLAIM_TTL = 300.0

_JSON_REPLY_INSTRUCTION = (
    "Return your final answer as a single bare JSON value (no code fences, no prose) "
    "matching this schema hint: {schema}"
)

_JOB_ERROR_MESSAGES = {
    "transcript_correlation_failed": (
        "Theater could not correlate this participant with its transcript. "
        "The agent may still be alive and working; do not retry the task, and inspect "
        "its pane before deciding what to do."
    ),
    "transcript_correlation_ambiguous": (
        "Theater found transcript output that is not uniquely attributable to this "
        "participant. The agent may still be alive and working; do not retry the task, "
        "and inspect its pane before deciding what to do."
    ),
    TRANSCRIPT_IDENTITY_LOST_CODE: (
        "Theater lost the trusted transcript identity for this participant. Screen status "
        "may still be live, but transcript attribution is quarantined; inspect candidates "
        "and rebind the participant before sending more work."
    ),
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE: (
        "The transcript source stayed unavailable past the observation grace. The pane may "
        "still be healthy; inspect it before retrying or replacing any binding."
    ),
}

CLAUDE_RECEIPT_RPC = "claude.receipt"
TRANSCRIPT_RECEIPT_RPC = "transcript.receipt"
TRANSCRIPT_RECEIPT_BUS_KIND = "agent.transcript_receipt"


def method(name: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        METHODS[name] = fn
        return fn

    return register


def _require(params: dict, key: str) -> Any:
    if key not in params or params[key] in (None, ""):
        raise BadRequest(f"missing required parameter {key!r}")
    return params[key]


def _validate_worktree_param(value: Any) -> str | bool | None:
    """Normalise and validate the ``worktree`` RPC parameter.

    Accepts ``True``, ``False``, ``None``, or a non-empty string. Rejects
    integers, lists, dicts, and empty strings so that truthiness never
    turns an unexpected type into a unique worktree.
    """
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, str):
        if not value.strip():
            raise BadRequest(
                "worktree name must be a non-empty string; an empty string is "
                "not a valid named-worktree name"
            )
        return value
    raise BadRequest(f"worktree parameter must be bool, str, or None; got {type(value).__name__}")


def _string_param(params: dict, key: str, *, method_name: str, allow_empty: bool = False) -> str:
    if key not in params or params[key] is None:
        raise BadRequest(f"{method_name} requires string parameter {key!r}")
    value = params[key]
    if not isinstance(value, str):
        raise BadRequest(f"{method_name} parameter {key!r} must be a string")
    if not allow_empty and value == "":
        raise BadRequest(f"{method_name} parameter {key!r} must be a non-empty string")
    return value


def _optional_string_param(params: dict, key: str, *, method_name: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequest(f"{method_name} parameter {key!r} must be a string or null")
    return value


def _reject_cross_participant_receipt(
    daemon,
    *,
    participant_id: str,
    harness: str,
    session_id: str,
    transcript_location: str,
) -> None:
    canonical_harness = normalize(harness)
    for other in daemon.registry.list(include_dead=True):
        if other.id == participant_id:
            continue
        if normalize(other.harness) != canonical_harness:
            continue
        if same_location(other.transcript_location, transcript_location):
            raise BadRequest("transcript receipt location is already owned by another participant")
        if other.session_id == session_id and provenance_at_least(
            other.session_correlation, TranscriptProvenance.OPERATOR
        ):
            raise BadRequest(
                "transcript receipt session_id is already owned by another participant"
            )


def _reject_unbound_same_cwd_receipt(
    daemon,
    *,
    participant_id: str,
    harness: str,
    participant_session_id: str | None,
    participant_location: str | None,
    session_id: str,
    transcript_location: str,
) -> None:
    participant = daemon.store.get_participant(participant_id)
    if participant is None or not participant.cwd:
        return
    # The launch token proves this caller can read the hook's private file; it
    # does not prove the harness's transcript is honest. A same-user process
    # with that token can still name a plausible transcript. The checks in the
    # plugin's validator bind the claim to the harness's own format rules and
    # records in the file. This additional guard refuses the dangerous
    # ambiguous case: a brand-new transcript claim while another live same-
    # harness participant could plausibly own the same cwd. With no
    # competitor, the remaining trust boundary is the local Unix user that
    # can read Theater's private token.
    cwd = Path(participant.cwd).resolve()
    canonical_harness = normalize(harness)
    for other in daemon.registry.list():
        if (
            other.id == participant_id
            or other.status is Status.DEAD
            or normalize(other.harness) != canonical_harness
            or not other.cwd
            or Path(other.cwd).resolve() != cwd
        ):
            continue
        if session_id == participant_session_id or same_location(
            participant_location, transcript_location
        ):
            return
        raise BadRequest(
            "transcript receipt cannot claim a new unbound transcript while "
            "another live participant of the same harness shares its cwd"
        )


def _serialized_response_format(params: dict) -> str | None:
    raw = params.get("response_format")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BadRequest(
            "response_format must be a JSON object or null; pass a schema hint "
            "object such as {'type': 'object'}"
        )
    return json.dumps(raw, sort_keys=True, separators=(",", ":"))


def _prompt_with_response_format(prompt: str, response_format: str | None) -> str:
    if response_format is None:
        return prompt
    return f"{_JSON_REPLY_INSTRUCTION.format(schema=response_format)}\n\n{prompt}"


def _reject_response_format_resume(
    harness_name: Any, resume: Any, response_format: str | None
) -> None:
    if response_format is None or not resume or not isinstance(harness_name, str):
        return
    harness = HARNESSES.get(normalize(harness_name))
    if harness is not None and not harness.resume_takes_prompt:
        raise BadRequest(
            f"harness {harness_name!r} cannot resume a session with response_format; "
            f"resume it without one and use send to deliver the task"
        )


def _caller_participant(daemon, params: dict, *, method_name: str):
    caller_id = _string_param(params, "caller_id", method_name=method_name)
    caller = daemon.store.get_participant(caller_id)
    if caller is None:
        raise BadRequest(f"{method_name} requires caller_id to name an existing participant")
    return caller


async def _repo_scope_for_store(caller) -> str:
    if not caller.cwd:
        raise BadRequest("scratchpad cannot be used outside a git repository: caller has no cwd")
    repo_root = await workers.to_thread(
        worktree.main_repo_root,
        caller.cwd,
        child_id=caller.id,
        label="store.repo_root",
    )
    if repo_root is None:
        raise BadRequest(
            "scratchpad cannot be used outside a git repository: caller cwd is not in a git repo"
        )
    return repo_root


def _transcript_identity_lost(daemon, pid: str) -> bool:
    checker = getattr(daemon.observer, "transcript_identity_lost", None)
    return bool(checker(pid)) if callable(checker) else False


def _resume_state(p: Participant, live_peers: list[Participant]) -> str:
    """Derive the resume verdict for one participant without extra DB queries.

    Covers the generic identity and capability gates spawn_session checks before
    delegating to the harness-specific ``resume_launch_overlay`` hook. It does
    **not** cover harness-specific resume validation: a markerless trusted dead
    Vibe row reports ``resumable`` here while the spawner would refuse it, and a
    predecessor with a mismatched transcript domain may pass here and fail in
    the hook. The verdict is an honest pre-flight, not a guarantee that spawn
    would succeed.

    The gates, in the order spawn_session hits them:

    1. ``live``                  — _resolve_resume_reference refuses if the
                                   named participant is still alive.
    2. ``no_session_id``         — _resolve_resume_reference refuses next when
                                   no harness session id has been recorded.
    3. ``harness_cannot_resume`` — check_resume (called from
                                   _validate_before_create) refuses before any
                                   identity check runs.
    4. ``owned_by_live``         — _validate_resume_identity scans all
                                   participants filtered to (harness match AND
                                   session_id match AND trusted provenance).
                                   If any such participant is live, it raises
                                   immediately.
    5. ``untrusted``             — _validate_resume_identity then raises
                                   when no trusted dead match exists.
    6. ``resumable``             — all generic gates passed; harness-specific
                                   validation in resume_launch_overlay may
                                   still refuse.

    ``live_peers`` must be the set of currently live participants so that the
    owned_by_live check can find peers sharing a session id.  Dead rows are
    never needed here: the spawner's live-peer check discards dead participants
    by construction (only non-dead rows enter live_matches).
    """
    if p.status is not Status.DEAD:
        return "live"
    if not p.session_id:
        return "no_session_id"
    # normalize(p.harness) mirrors every other callsite in this module that
    # looks up a harness by name (e.g. methods.py:1519, :1857).  Without it a
    # stored alias (e.g. "claude-code") would miss the registry entry and
    # falsely report harness_cannot_resume.
    harness = HARNESSES.get(normalize(p.harness))
    # harness is None means an unrecognised harness name — the adapter is gone
    # or the row predates it.  We fold this into harness_cannot_resume rather
    # than inventing a separate state: the caller cannot resume either way, and
    # the remedy (install/re-register the adapter) is the same.  This is a
    # deliberate conflation, kept because a new state would widen the protocol
    # without adding actionable information.
    if harness is None or not supports_resume(harness):
        return "harness_cannot_resume"
    # owned_by_live must be checked BEFORE untrusted.  _validate_resume_identity
    # filters to (harness match AND session_id match AND trusted provenance)
    # before splitting into live_matches / dead_matches.  An untrusted subject
    # row with a trusted live peer: the peer enters live_matches, the subject
    # enters neither list; live_matches fires first (gate 442), not 451.
    # Checking owned_by_live first here matches that order exactly.
    for other in live_peers:
        if (
            normalize(other.harness) == normalize(p.harness)
            and other.session_id == p.session_id
            and is_trusted_provenance(other.session_correlation)
        ):
            return "owned_by_live"
    if not is_trusted_provenance(p.session_correlation):
        return "untrusted"
    return "resumable"


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

    # Validate the optional ids filter — follow the pattern from _recall.
    raw_ids = params.get("ids")
    if raw_ids is None:
        ids: list[str] | None = None
    else:
        if not isinstance(raw_ids, list):
            raise BadRequest("ids must be a list of non-empty strings, or absent")
        for item in raw_ids:
            if not isinstance(item, str) or not item:
                raise BadRequest(
                    "ids must be a list of non-empty strings; "
                    "an empty string would widen the query to all rows"
                )
        if len(raw_ids) > 200:
            raise BadRequest("ids list is capped at 200 entries")
        ids = raw_ids

    # Fetch the requested page first.
    page = daemon.registry.list(include_dead=include_dead, ids=ids)

    # live_peers is only needed when page can contain a dead row.  When
    # include_dead=False every row in page satisfies status is not DEAD, so
    # _resume_state returns "live" at its first check and never reaches the
    # peer loop — live_peers would be dead weight.  Gate the query on
    # include_dead so the flagship case (ids=[...], include_dead=False) costs
    # exactly one query, not two.
    #
    # When include_dead=True and ids=None, page is already the full live+dead
    # set; we still need the live-only subset for the peer loop, so one extra
    # query is unavoidable.  When include_dead=True and ids is set, page is a
    # subset and live_peers must be the unrestricted live set so that peers
    # outside the requested ids are still found.
    live_peers = daemon.registry.list(include_dead=False) if include_dead else []

    result = []
    for p in page:
        d = p.to_dict()
        d["resume_state"] = _resume_state(p, live_peers)
        result.append(d)
    return result


@method("participants.recent_dead")
async def _recent_dead(daemon, params: dict) -> list[dict]:
    limit = params.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise BadRequest("limit must be an integer between 1 and 20")
    rows = daemon.store.list_recent_dead(limit=limit)
    live_peers = daemon.registry.list(include_dead=False)
    ids = [p.id for p in rows]
    prompts = daemon.store.spawn_prompts_for_targets(ids)
    result = []
    for p in rows:
        record = p.to_dict()
        record["resume_state"] = _resume_state(p, live_peers)
        record["spawn_prompt"] = prompts.get(p.id)
        result.append(record)
    return result


@method("participants.tree")
async def _tree(daemon, params: dict) -> list[dict]:
    return daemon.registry.tree()


@method("participants.get")
async def _get(daemon, params: dict) -> dict:
    return daemon.registry.resolve(_require(params, "id")).to_dict()


@method("participant.rename")
async def _rename(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    name = _require(params, "name")
    return daemon.registry.rename(pid, name).to_dict()


@method("participant.status")
async def _status(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    raw = _require(params, "status")
    try:
        status = Status(raw)
    except ValueError:
        raise BadRequest(f"unknown status {raw!r}") from None
    target = daemon.registry.resolve(pid)
    daemon.registry.set_status(target.id, status)
    return daemon.registry.get(target.id).to_dict()


@method(TRANSCRIPT_RECEIPT_RPC)
async def _transcript_receipt(daemon, params: dict) -> dict:
    """Authenticated receipt of a harness's current transcript identity.

    Generic: core handles token auth, liveness, ownership-conflict policy,
    persistence, the bus audit event, watcher admission, and token renewal.
    The harness plugin's ``validate_transcript_receipt`` hook handles every
    format-specific concern (field names, path rules, record scans).
    """
    pid = _string_param(params, "id", method_name=TRANSCRIPT_RECEIPT_RPC)
    token = _string_param(params, "token", method_name=TRANSCRIPT_RECEIPT_RPC)
    raw_payload = params.get("payload")
    if not isinstance(raw_payload, dict):
        raise BadRequest(f"{TRANSCRIPT_RECEIPT_RPC} parameter 'payload' must be a JSON object")

    participant = daemon.store.get_participant(pid)
    if participant is None:
        raise BadRequest("transcript receipt id does not name an existing participant")
    if participant.status is Status.DEAD:
        daemon.store.delete_receipt_token(pid)
        raise BadRequest("transcript receipt id names a dead participant")
    expected = daemon.store.get_receipt_token(pid)
    if expected is None or not hmac.compare_digest(token, expected):
        raise BadRequest("transcript receipt token is invalid")

    # Resolve the observer through the daemon's harness registry, not the
    # global HARNESSES — tests inject a daemon-local observer.
    harness = daemon.observer.harnesses.get(normalize(participant.harness))
    observer = getattr(harness, "observer", None) if harness is not None else None
    if observer is None:
        raise BadRequest(
            f"transcript receipt: no observer registered for harness {participant.harness!r}"
        )

    # The plugin validates the opaque payload. Rejection is an exception
    # (ValueError), never a candidate carrying rejection_reason. Core catches
    # only ValueError and maps it to BadRequest.
    try:
        candidate = observer.validate_transcript_receipt(
            payload=raw_payload,
            cwd=participant.cwd,
            expected_session_id=participant.session_id,
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc

    # Core validates the returned candidate. A plugin returning junk must get
    # a clean error, not an obscure failure downstream.
    if not isinstance(candidate, TranscriptCandidate):
        raise BadRequest(
            f"transcript receipt validator must return a TranscriptCandidate, "
            f"got {type(candidate).__name__}"
        )
    if not isinstance(candidate.location, str) or not candidate.location:
        raise BadRequest("transcript receipt validator returned an empty location")
    if not isinstance(candidate.session_id, str) or not candidate.session_id:
        raise BadRequest("transcript receipt validator returned an empty session_id")
    if candidate.rejection_reason:
        raise BadRequest(
            f"transcript receipt validator returned a rejection: {candidate.rejection_reason}"
        )

    location = candidate.location
    session_id = candidate.session_id
    _reject_cross_participant_receipt(
        daemon,
        participant_id=pid,
        harness=participant.harness,
        session_id=session_id,
        transcript_location=location,
    )
    _reject_unbound_same_cwd_receipt(
        daemon,
        participant_id=pid,
        harness=participant.harness,
        participant_session_id=participant.session_id,
        participant_location=participant.transcript_location,
        session_id=session_id,
        transcript_location=location,
    )
    admission = daemon.observer.transcript_receipt(pid, location=location, session_id=session_id)
    daemon.store.renew_receipt_token(pid)
    daemon.store.bus_append(
        TRANSCRIPT_RECEIPT_BUS_KIND,
        to_id=pid,
        payload={"location": location, "session_id": session_id, "admission": admission},
    )
    return {"ok": True, "admission": admission}


@method(CLAUDE_RECEIPT_RPC)
async def _claude_receipt_alias(daemon, params: dict) -> dict:
    """Backward-compatible alias: live Claude sessions invoke this name.

    Shipped in v3.2.0 with settings.json on disk referencing
    ``claude.receipt`` by that exact name. Forwards ``session_id`` and
    ``transcript_path`` into the generic ``transcript.receipt`` payload.
    """
    session_id = params.get("session_id")
    transcript_path = params.get("transcript_path")
    if session_id is not None and not isinstance(session_id, str):
        raise BadRequest("claude.receipt parameter 'session_id' must be a string")
    if transcript_path is not None and not isinstance(transcript_path, str):
        raise BadRequest("claude.receipt parameter 'transcript_path' must be a string")
    forwarded = dict(params)
    forwarded["payload"] = {
        k: v
        for k, v in (
            ("session_id", session_id),
            ("transcript_path", transcript_path),
        )
        if v is not None
    }
    return await _transcript_receipt(daemon, forwarded)


@method("participant.kill")
async def _kill(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    caller_id = params.get("caller_id") or "cli"

    target = daemon.registry.resolve(pid)
    pid = target.id

    if caller_id != "cli":
        if target.id == caller_id:
            raise NoSelfKill(f"refusing to kill {pid!r}: that is you, not your child")
        if target.parent_id != caller_id:
            raise NotYourChild(
                f"refusing to kill {pid!r}: its parent is "
                f"{target.parent_id!r}, not you ({caller_id!r})"
            )
        if target.status is Status.DEAD:
            return {"id": pid, "killed": False, "reason": "already_dead"}

    # `finish` is first-terminal-write-wins, so whoever writes first owns the
    # state. The marker tells the reaper to leave this participant's jobs to
    # us, and it has to stay set until the KILLED writes are done — not just
    # until the pane is gone — or a reaper tick between the two could still
    # land CRASHED. It is dropped in the finally so a failed kill leaks nothing.
    daemon._explicit_kills.add(pid)
    try:
        participant = await daemon.spawner.kill_pane(pid)
        # Finish jobs before teardown: job completion hashes files in the
        # worktree, and teardown deletes the directory. The pane is gone by
        # here — kill_pane raises if it survives, so this path is reached
        # only after the pane is confirmed dead.
        for job in daemon.store.running_jobs_for_target(pid):
            daemon.jobs.finish(job.handle, state=JobState.KILLED, error_code="killed")
        await daemon.spawner.teardown(participant)
    finally:
        daemon._explicit_kills.discard(pid)

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
    harness = (
        normalize(override) if override else detect_harness(match.current_command, match.pane_pid)
    )
    if cwd is None:
        cwd = match.cwd
    participant = daemon.registry.register(
        harness=harness,
        pane=pane,
        cwd=cwd,
    )
    # The launch epoch is from the shell tmux forked, not the harness — the
    # harness is its descendant, so it stays constant when the CLI exits.
    # The delivery gate cannot rely on the pid alone.
    participant = daemon.registry.attach_pane(participant.id, pane, pane_pid=match.pane_pid)
    return participant.to_dict()


@method("participants.unmanaged")
async def _unmanaged(daemon, params: dict) -> list[dict]:
    """Panes running a known harness binary with no participant record."""
    if not tmux.available():
        return []
    panes = await tmux.list_panes()
    registered = {p.tmux_pane for p in daemon.registry.list() if p.tmux_pane}
    candidates = [p for p in panes if p.pane_id not in registered]

    # A pane whose foreground command IS the harness binary (the common case)
    # needs no process walk at all. Only capture the machine's process table
    # — one `ps`, off the event loop — when some candidate needs it, and reuse
    # that single snapshot for every one of them rather than one `ps` each.
    needs_walk = any(match_binary(p.current_command, HARNESSES) is None for p in candidates)
    snapshot = (
        await workers.to_thread(proc.ProcessSnapshot.capture, label="unmanaged.capture")
        if needs_walk
        else None
    )

    out: list[dict] = []
    for p in candidates:
        harness = detect_harness(p.current_command, p.pane_pid, snapshot)
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
    response_format = _serialized_response_format(params)
    harness_name = _require(params, "harness")
    _reject_response_format_resume(harness_name, params.get("resume"), response_format)
    prompt = _prompt_with_response_format(params.get("prompt") or "", response_format)
    req = SpawnRequest(
        harness=harness_name,
        prompt=prompt,
        cwd=_require(params, "cwd"),
        approval=_require(params, "approval"),
        parent_id=params.get("parent_id"),
        tmux_session=params.get("tmux_session"),
        window_name=params.get("window_name"),
        background=params.get("background", True),
        worktree=_validate_worktree_param(params.get("worktree", False)),
        base_branch=params.get("base_branch"),
        model=params.get("model"),
        reasoning_effort=params.get("reasoning_effort"),
        resume=params.get("resume"),
        response_format=response_format,
    )
    rails = daemon.config.rails
    check_depth(daemon.store, req.parent_id, cap=rails.depth_cap)
    check_budget(daemon.store, req.parent_id, limit=rails.budget)
    # Policy, not capability: `Spawner` asks the adapter whether it can take
    # a model; whether the user permits this model is a question only the
    # config can answer, and the spawner has none.
    check_model_allowed(req.harness, req.model, daemon.config.models_for(req.harness))
    check_reasoning_allowed(
        req.harness, req.reasoning_effort, daemon.config.reasoning_for(req.harness)
    )

    # Reserve the participant, worktree, plan, and config files — but not
    # the tmux pane. The job is created between reserve and launch so it is
    # RUNNING before the pane can produce output, closing the race where a
    # fast child completes before the job exists and the observer sees a
    # turn end with no job to receive the result.
    reservation = await daemon.spawner.reserve(req)
    handle = reservation.participant.id
    launched = False
    try:
        daemon.jobs.create(
            handle=handle,
            caller_id=params.get("parent_id") or "cli",
            target_id=reservation.participant.id,
            kind="spawn",
            prompt=req.prompt or "",
            # participant.cwd is the worktree path when worktree=True, or the
            # requested cwd otherwise. Hashing against the parent repo when the
            # child was in a worktree would resolve paths to the wrong files.
            cwd=reservation.participant.cwd,
            response_format=response_format,
        )
        participant = await daemon.spawner.launch(reservation)
        launched = True
        if not req.prompt:
            # A promptless spawn has nothing to wait for: resolving the job
            # here keeps it from eating the first turn end the human produces,
            # and from counting as work in flight that would block every
            # `send`. Done *after* launch succeeds so a launch failure still
            # leaves the job CRASHED, not DONE.
            daemon.jobs.finish(handle, state=JobState.DONE, result="")
    except BaseException:
        if not launched:
            await daemon.spawner.cleanup_reservation(reservation.participant)
        job = daemon.jobs.get(handle)
        if job is not None and job.state == JobState.RUNNING:
            daemon.jobs.finish(
                handle,
                state=JobState.CRASHED,
                result="",
                error_code="spawn_failed",
            )
        raise
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

    Both also run before anything is written to the bus: a call that is refused
    never happened, and must leave no trace for the régie to animate.

    The emission rule, in one place: one `job.await.start` per awaited job that
    names a target, written only once a call from a known caller has been
    blocked for `AWAIT_ANNOUNCE_AFTER` — and exactly one `job.await.end` for
    each start that reached the bus, whether the await returned, timed out, or
    raised. No start, no end.
    """
    handles = params.get("handles") or []
    if not handles:
        raise BadRequest("at least one handle is required")
    max_wait = min(max(float(params.get("max_wait", 150.0)), 0.0), MAX_AWAIT)
    caller_id = params.get("caller_id")

    known = {h: daemon.jobs.get(h) for h in handles}
    # Cycles are about participants, but a send handle is `<target>#<n>`.
    # Resolve through jobs; a handle with no job can still name a participant
    # (a spawn handle is its own participant id).
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

    # An await is worth announcing only if it can really block: `await_jobs`
    # returns at entry the moment any requested job is already terminal, so
    # "every known job is RUNNING" is the whole test. It is also why the edge
    # list does not re-check state — when it is built, nothing is terminal.
    known_jobs = [job for job in known.values() if job is not None]
    will_block = max_wait > 0 and all(job.state == JobState.RUNNING for job in known_jobs)
    await_edges: list[tuple[str, str]] = []
    if caller_id and will_block:
        await_edges = [
            (handle, job.target_id)
            for handle, job in known.items()
            if job is not None and job.target_id
        ]

    await_token = new_id()
    #: Edges whose `job.await.start` reached the bus, and so must be closed.
    announced: list[tuple[str, str]] = []
    try:
        with daemon.jobs.waiting(caller_id, targets):
            jobs = await _await_announced(
                daemon,
                handles=handles,
                max_wait=max_wait,
                caller_id=caller_id,
                edges=await_edges,
                token=await_token,
                announced=announced,
            )
    finally:
        _close_await(daemon, caller_id, announced, await_token)
    rows = []
    for job in jobs:
        row = job.to_dict()
        message = _JOB_ERROR_MESSAGES.get(job.error_code or "")
        if message is not None:
            row["error"] = message
        rows.append(row)
    return rows


async def _await_announced(
    daemon,
    *,
    handles: list[str],
    max_wait: float,
    caller_id: str | None,
    edges: list[tuple[str, str]],
    token: str,
    announced: list[tuple[str, str]],
) -> list[Job]:
    """Wait for the jobs, announcing the wait only if it lasts long enough.

    The wait runs as a task raced against the announce delay rather than being
    preceded by a sleep: an await that is answered in 5ms must still return in
    5ms. What the caller gets back is whatever `await_jobs` returned; what the
    bus gets is a start row per edge, and only once the call has really been
    blocked. `announced` comes from the caller because closing those rows is
    the caller's `finally` — this function can exit by exception too.
    """
    waiter = asyncio.create_task(daemon.jobs.await_jobs(handles, max_wait=max_wait))
    try:
        if edges:
            finished, _ = await asyncio.wait({waiter}, timeout=AWAIT_ANNOUNCE_AFTER)
            if not finished:
                _open_await(daemon, caller_id, edges, token, announced)
        return await waiter
    finally:
        # A cancelled RPC (the client hung up) must not leave the wait running.
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


def _open_await(
    daemon,
    caller_id: str | None,
    edges: list[tuple[str, str]],
    token: str,
    announced: list[tuple[str, str]],
) -> None:
    """Announce a blocked await, recording every row that reached the bus.

    `announced` is appended to *after* the insert returns, never before: a row
    whose insert raised does not exist, and closing a start nobody saw would be
    a phantom. The caller closes exactly what this list holds, so a disk error
    halfway through a multi-handle await leaves no start without its end.
    """
    for handle, target_id in edges:
        daemon.store.bus_append(
            "job.await.start",
            from_id=caller_id,
            to_id=target_id,
            payload={"handle": handle, "token": token},
        )
        announced.append((handle, target_id))


def _close_await(
    daemon,
    caller_id: str | None,
    announced: list[tuple[str, str]],
    token: str,
) -> None:
    """Close every start row that was written, however the await ended.

    Best effort per row, because this runs in a `finally`: an exception raised
    here would replace the one already on its way out to the caller, and one
    unwritable row must not stop the others from closing. A start with no end
    is an animation the régie never stops drawing.
    """
    for handle, target_id in announced:
        try:
            daemon.store.bus_append(
                "job.await.end",
                from_id=caller_id,
                to_id=target_id,
                payload={"handle": handle, "token": token},
            )
        except Exception:
            logger.exception("could not close await %s on %s", token, handle)


@method("jobs.status")
async def _jobs_status(daemon, params: dict) -> dict:
    """Get the current state of a single job."""
    handle = _require(params, "handle")
    job = daemon.jobs.get(handle)
    if job is None:
        raise BadRequest(f"no job {handle!r}")
    return job.to_dict()


@method("scratchpad.write")
async def _scratchpad_write(daemon, params: dict) -> dict:
    caller = _caller_participant(daemon, params, method_name="scratchpad.write")
    namespace = _string_param(params, "namespace", method_name="scratchpad.write")
    value = _string_param(params, "value", method_name="scratchpad.write", allow_empty=True)
    key = _optional_string_param(params, "key", method_name="scratchpad.write")
    minted = daemon.store.scratchpad_write(
        tree_root_id=lineage.root_of(daemon.store, caller.id),
        repo_root=await _repo_scope_for_store(caller),
        namespace=namespace,
        value=value,
        updated_by=caller.id,
        key=key,
    )
    return {"namespace": namespace, "key": minted}


@method("scratchpad.get")
async def _scratchpad_get(daemon, params: dict) -> dict:
    caller = _caller_participant(daemon, params, method_name="scratchpad.get")
    namespace = _string_param(params, "namespace", method_name="scratchpad.get")
    keys_raw = params.get("keys")
    if keys_raw is None:
        keys: list[str] | None = None
    elif isinstance(keys_raw, list) and all(isinstance(k, str) for k in keys_raw):
        keys = keys_raw
    else:
        raise BadRequest("scratchpad.get parameter 'keys' must be a list of strings or null")
    entries = daemon.store.scratchpad_get(
        tree_root_id=lineage.root_of(daemon.store, caller.id),
        repo_root=await _repo_scope_for_store(caller),
        namespace=namespace,
        keys=keys,
    )
    return {"namespace": namespace, "entries": entries}


@method("bus.tail")
async def _bus_tail(daemon, params: dict) -> list[dict]:
    return daemon.store.bus_tail(
        limit=int(params.get("limit", 100)), after_id=int(params.get("after_id", 0))
    )


def _retention_floor(daemon) -> dict:
    """The oldest data actually present, per source.

    Returns {"jobs_from": float | None, "bus_from": float | None} — the
    earliest timestamp each table still holds, or None when the table is
    empty. Two floors rather than one because the two are backed by
    different tables under different retention: jobs outlive bus events by
    a wide margin, so a single number would misdescribe one of them.

    This is what stats can honestly speak about, as distinct from what the
    caller asked for.
    """
    jobs_floor = daemon.store.conn.execute(select(func.min(jobs.c.created_at))).scalar()
    bus_floor = daemon.store.conn.execute(select(func.min(bus.c.ts))).scalar()
    return {"jobs_from": jobs_floor, "bus_from": bus_floor}


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
        "coverage": _retention_floor(daemon),
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


async def _check_pane_identity(daemon, target, refuse: Callable[..., NoReturn]) -> None:
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
            StaleTarget(f"pane {target.tmux_pane} of {target.id!r} no longer exists"),
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

    found = detect_harness(pane.current_command, pane.pane_pid)
    verdict = compare_detected_harness(normalize(target.harness), found, pane.current_command)
    if verdict is PaneHarnessVerdict.MATCH:
        return
    if verdict is PaneHarnessVerdict.CONFLICT:
        refuse(
            StaleTarget(
                f"pane {target.tmux_pane} is running {found!r}, "
                f"not {target.harness!r}; {target.id!r} has lost its seat"
            ),
            reason="harness_changed",
        )
    if verdict is PaneHarnessVerdict.HARNESS_GONE:
        refuse(
            StaleTarget(
                f"{target.harness} has exited in pane {target.tmux_pane}; "
                f"a shell ({pane.current_command}) is at the prompt"
            ),
            reason="harness_gone",
        )


async def _check_approval_modal(daemon, target, refuse: Callable[..., NoReturn]) -> None:
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
        capture = await tmux.run("capture-pane", "-p", "-t", target.tmux_pane, check=False)
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


def _check_transcript_send_preflight(daemon, target, refuse: Callable[..., NoReturn]) -> None:
    """Refuse sends whose transcript attribution is absent or quarantined.

    Adopted transcript-backed panes start screen-observable but untrusted; a
    bound participant can later become quarantined if the trusted pin loses
    identity. Both refusals happen here, before job creation.
    """
    if _transcript_identity_lost(daemon, target.id):
        refuse(
            TranscriptIdentityLost(transcript_identity_recovery_message(target.id)),
            reason=TRANSCRIPT_IDENTITY_LOST_CODE,
        )
        return
    if target.tier is not Tier.ADOPTED or is_trusted_provenance(target.session_correlation):
        return
    harness = HARNESSES.get(normalize(target.harness))
    if harness is None or not harness.observer.has_transcript:
        return
    pid = target.id
    refuse(
        TranscriptUntrusted(
            f"participant {pid!r} is adopted, but its transcript identity is not yet "
            "operator/proven/exact. Screen-only status observation remains live, but "
            "Theater will not create a send job until attribution is trusted. Run "
            f"`theater candidates {pid}` to inspect candidates, then "
            f"`theater bind {pid} <candidate> --confirm-id {pid}` for the candidate "
            "you verified. If no candidates are listed yet, retry after the next "
            "observation poll before binding."
        ),
        reason="transcript_untrusted",
    )


@method("send")
async def _send(daemon, params: dict) -> dict:
    """Send a prompt to an already-running agent by pasting into its pane."""
    target = daemon.registry.resolve(_require(params, "target"))
    target_id = target.id
    response_format = _serialized_response_format(params)
    prompt = _prompt_with_response_format(_require(params, "prompt"), response_format)
    caller_id = params.get("caller_id") or "cli"

    refuse = functools.partial(_refuse_send, daemon, caller_id=caller_id, target_id=target_id)

    if not target.addressable:
        refuse(
            NotAddressable(f"participant {target_id!r} is not addressable (tier={target.tier})"),
            reason="not_addressable",
        )
    if not target.tmux_pane:
        refuse(
            NotAddressable(f"participant {target_id!r} has no pane to send to"),
            reason="no_pane",
        )

    # Pane identity before presence: the pane must still be the participant's.
    await _check_pane_identity(daemon, target, refuse)

    if await human_present(target.tmux_pane):
        refuse(
            HumanPresent(f"a human is present at {target.tmux_pane}; not injecting"),
            reason="human_present",
        )

    # Costs a capture-pane, so runs after the cheaper presence check.
    await _check_approval_modal(daemon, target, refuse)

    _check_transcript_send_preflight(daemon, target, refuse)

    # Busy is any running job that carried a prompt — a spawn prompt
    # occupies the pane exactly as a send does. Status is deliberately not
    # consulted: it is inferred from a transcript and has been wrong
    # before; a stuck WORKING would silently make a participant
    # unreachable, which is worse than a prompt landing mid-turn.
    # A job that has held its reservation past SEND_CLAIM_TTL is dropped
    # from this check: the observer may still answer it, but it no longer
    # blocks the pane.
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

    # Reserve before typing: the check above and this create must not be
    # separated by an await, or two sends racing through the daemon both
    # read an empty queue and both type into the pane. A fast agent can
    # finish its turn before the RPC returns, so the job must exist first
    # or the observer sees the turn end with nothing to answer.
    handle = f"{target_id}#{daemon._next_send_seq()}"
    daemon.jobs.create(
        handle=handle,
        caller_id=caller_id,
        target_id=target_id,
        kind="send",
        prompt=prompt,
        # The target's cwd, not the caller's: the target's tool calls touch
        # files relative to where it runs.
        cwd=target.cwd,
        response_format=response_format,
    )
    try:
        await tmux.deliver_text(target.tmux_pane, prompt)
    except Exception as exc:
        # Nothing was delivered, so nothing will ever answer. Close the job
        # rather than leave a reservation that blocks the next send.
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
                "reasoning": daemon.config.reasoning_for(name),
                "reasoning_supported": harness is not None and supports_reasoning(harness),
                "installed": row["installed"],
                "error": row["error"],
            }
        )
    return rows


@method("gc")
async def _gc(daemon, params: dict) -> dict:
    """Run a garbage-collection sweep on demand and report what it did.

    The automatic ``_gc_loop`` runs ``sweep`` every ``retention.interval``
    seconds; this method is for a user who wants it *now*, or who wants to
    reclaim disk space with ``--vacuum``.

    Deleting rows does not shrink the database file — measured, deleting 94%
    of the bus table left the file the same size (it grew, because of the
    WAL). Only ``VACUUM`` reclaims space, by rewriting the whole file under
    an exclusive lock. So the response carries before/after byte sizes so the
    caller can report what was actually reclaimed, and a ``vacuum_ran`` flag.

    Response keys::

        {
            "bus": int,              # rows deleted from the bus table
            "jobs": int,             # finished jobs deleted
            "touch": int,            # touch rows deleted (with their jobs)
            "participants": int,     # dead participants deleted
            "running_marked": int,   # stale running jobs marked crashed
            "scratchpad": int,       # scratchpad rows deleted
            "coverage": {            # retention floors *after* the sweep
                "jobs_from": float | None,
                "bus_from": float | None,
            },
            "db_bytes_before": int,   # file size before the sweep
            "db_bytes_after": int,   # file size after everything
            "vacuum_ran": bool,       # whether VACUUM was called
        }
    """
    from theater.daemon.gc import sweep, vacuum

    # Run the sweep even when retention.enabled is false: that setting governs
    # the automatic loop, not an explicit user command. Refusing would surprise
    # a user who typed `theater gc` expecting it to do something.
    db_path = paths.db_path()
    db_bytes_before = db_path.stat().st_size if db_path.exists() else 0

    result = await sweep(
        daemon.store,
        daemon.config.retention,
        live_handles=frozenset(daemon.jobs._events),
    )

    vacuum_ran = bool(params.get("vacuum", False))
    if vacuum_ran:
        # Vacuum after the sweep: vacuuming before would rewrite the file
        # including rows about to be deleted.
        vacuum(daemon.store)

    db_bytes_after = db_path.stat().st_size if db_path.exists() else 0

    return {
        "bus": result.bus,
        "jobs": result.jobs,
        "touch": result.touch,
        "participants": result.participants,
        "running_marked": result.running_marked,
        "scratchpad": result.scratchpad,
        "coverage": _retention_floor(daemon),
        "db_bytes_before": db_bytes_before,
        "db_bytes_after": db_bytes_after,
        "vacuum_ran": vacuum_ran,
    }


@method("shutdown")
async def _shutdown(daemon, params: dict) -> dict:
    daemon.stop()
    return {"stopping": True}


#: Kinds `read_transcript` reports. ERROR is dropped: a harness-level error
#: record is not part of the conversation.
_READABLE = ("assistant", "user", "tool_call", "tool_result")


def _candidate_owner(daemon, location: str, *, exclude: str | None = None):
    for other in daemon.registry.list(include_dead=True):
        if other.id != exclude and same_location(other.transcript_location, location):
            return other
    return None


def _candidate_to_dict(daemon, candidate) -> dict:
    location = canonical_location(candidate.location)
    owner = _candidate_owner(daemon, location)
    return {
        "location": location,
        "session_id": candidate.session_id,
        "mtime": candidate.mtime,
        "size": candidate.size,
        "provenance": candidate.provenance,
        "rejection_reason": candidate.rejection_reason,
        "domain": candidate.domain,
        "owner": owner.id if owner is not None and owner.status is not Status.DEAD else None,
        "tombstone": owner.id if owner is not None and owner.status is Status.DEAD else None,
    }


@method("transcript.candidates")
async def _transcript_candidates(daemon, params: dict) -> dict:
    p = daemon.registry.resolve(_require(params, "id"))
    harness_name = normalize(p.harness)
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise BadRequest(f"cannot enumerate candidates: harness {p.harness!r} is not known")
    after = p.created_at if p.tier is Tier.SPAWNED else None
    rows = enumerate_transcript_candidates(
        harness.observer,
        cwd=p.cwd,
        domain=p.transcript_domain,
        after=after,
    )
    return {"id": p.id, "candidates": [_candidate_to_dict(daemon, row) for row in rows]}


@method("transcript.bind")
async def _transcript_bind(daemon, params: dict) -> dict:
    target = _string_param(params, "id", method_name="transcript.bind")
    p = daemon.registry.resolve(target)
    pid = p.id
    if params.get("confirm_id") != pid:
        raise BadRequest("transcript.bind requires confirm_id to equal the stable participant id")
    raw_candidate = _string_param(params, "candidate", method_name="transcript.bind")
    transfer_from = _optional_string_param(params, "transfer_from", method_name="transcript.bind")
    if transfer_from is not None and params.get("transfer_confirm_id") != transfer_from:
        raise BadRequest(
            "transcript.bind transfer requires transfer_confirm_id to equal transfer_from"
        )

    harness_name = normalize(p.harness)
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise BadRequest(f"cannot bind transcript: harness {p.harness!r} is not known")
    try:
        admitted = harness.observer.admit_operator_candidate(
            cwd=p.cwd,
            candidate=raw_candidate,
            domain=p.transcript_domain,
            after=p.created_at if p.tier is Tier.SPAWNED else None,
        )
    except ValueError as exc:
        raise BadRequest(f"cannot bind transcript: {exc}") from None
    location = canonical_location(admitted.location)
    owner = _candidate_owner(daemon, location, exclude=pid)
    prior_owner = None
    if owner is not None:
        prior_owner = owner.id
        if transfer_from is None:
            raise BadRequest(
                f"candidate is already owned by participant {owner.id}; "
                "pass --transfer-from with that exact stable id to move it"
            )
        if transfer_from != owner.id:
            raise BadRequest(
                f"transfer-from must name the current owner exactly ({owner.id}), "
                f"got {transfer_from!r}"
            )
    elif transfer_from is not None:
        raise BadRequest("transfer-from was provided but the candidate has no current owner")

    p = daemon.registry.get(pid)
    p.transcript_location = location
    p.session_id = admitted.session_id
    p.session_correlation = str(TranscriptProvenance.OPERATOR)
    p.transcript_domain = admitted.domain
    p.last_activity = now()
    audit_payload = {
        "actor_surface": "cli",
        "target": pid,
        "path": location,
        "session_id": admitted.session_id,
        "prior_owner": prior_owner,
    }
    daemon.store.bind_operator_transcript(
        target=p,
        prior_owner=owner,
        audit_payload=audit_payload,
    )
    if prior_owner is not None:
        await daemon.observer.reset_for_operator_bind(prior_owner)
    await daemon.observer.reset_for_operator_bind(pid)
    daemon.observer.record_operator_binding(
        pid,
        location,
        admitted.session_id,
        prior_owner=prior_owner,
    )
    return {
        "id": pid,
        "location": location,
        "session_id": admitted.session_id,
        "prior_owner": prior_owner,
    }


@method("read_transcript")
async def _read_transcript(daemon, params: dict) -> dict:
    """Read a participant's session back, with the text unclipped.

    Goes through the observer's `Source`, not through `find_transcript`, so an
    adapter whose output is a database answers this as well as one that writes
    a file. The source opened here is short-lived and separate from the
    watcher's: reading history must not move the watcher's cursor.
    """
    p = daemon.registry.resolve(_require(params, "id"))
    pid = p.id
    last_n = int(params.get("last_n", 5))

    harness_name = normalize(p.harness)
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise BadRequest(f"cannot read transcript: harness {p.harness!r} is not known")
    if _transcript_identity_lost(daemon, pid):
        raise TranscriptIdentityLost(transcript_identity_recovery_message(pid))

    # Same birth-time floor as the watch path (observer._open_source): the floor
    # applies to SPAWNED participants only, because an adopted session's output
    # predates Theater's first sight of it. Preserving that distinction exactly
    # — do not apply a floor to adopted or external participants.
    after = p.created_at if p.tier is Tier.SPAWNED else None
    source = open_participant_source(
        harness.observer,
        participant_id=p.id,
        cwd=p.cwd,
        session_id=p.session_id,
        after=after,
        session_provenance=normalize_provenance(p.session_correlation),
        known_location=p.transcript_location,
        transcript_domain=p.transcript_domain,
        pane_pid=p.live_pid,
    )
    try:
        history = await source.history(last_n=last_n)
    finally:
        await source.aclose()

    if history.error_code is not None:
        if history.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
            if p.status is Status.DEAD:
                raise BadRequest(
                    "cannot read transcript: trusted dead binding is retained for resume, "
                    "but its transcript is unavailable"
                )
            raise TranscriptIdentityLost(transcript_identity_recovery_message(pid, history.error))
        raise BadRequest(
            f"cannot read transcript: {history.error or history.error_code} ({history.error_code})"
        )
    if history.location is None:
        raise BadRequest("cannot read transcript: transcript no longer exists on disk")
    if not is_trusted_provenance(history.correlation):
        raise BadRequest(
            "cannot read transcript: this session is known only from cwd/time; "
            "wait for exact/proven correlation or bind the session before reading it "
            "(transcript_correlation_untrusted)"
        )
    if daemon.observer.history_is_ambiguous(pid, history):
        raise BadRequest(
            "cannot read transcript: its session is known only from cwd/time and another "
            "live participant of the same harness shares that transcript root and cwd "
            "(transcript_correlation_ambiguous)"
        )
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


@method("recall")
async def _recall(daemon, params: dict) -> dict:
    """Per-file timelines of what Theater watched happen.

    A join over ``touch``, ``jobs`` and ``participants``, ordered by
    ``finished_at`` descending per path. Gap detection is pure SQL: a
    row whose ``sha_before`` does not match the previous row's
    ``sha_after`` marks a transition no job claims. Two subprocess
    calls per query regardless of path count — see
    ``theater.daemon.recall`` for the budget.
    """
    from theater.daemon.recall import _dirty_set, _git_root
    from theater.daemon.recall import recall as _do_recall

    paths = _require(params, "paths")
    if not isinstance(paths, list) or not paths:
        raise BadRequest("paths must be a non-empty list")
    depth = int(params.get("depth", 5))
    caller_cwd = params.get("caller_cwd")
    # Pre-fetch the two git calls off the event loop, then pass them in
    # so the sync body of recall() forks nothing.
    effective_cwd = caller_cwd or str(Path.cwd())
    precomputed_root = await workers.to_thread(_git_root, effective_cwd, label="recall.git_root")
    precomputed_dirty = await workers.to_thread(_dirty_set, effective_cwd, label="recall.dirty_set")
    result = _do_recall(
        daemon.store,
        paths=paths,
        depth=depth,
        caller_cwd=caller_cwd,
        precomputed_root=precomputed_root,
        precomputed_dirty=precomputed_dirty,
    )
    _attach_parent_names(daemon, result)
    return result


def _attach_parent_names(daemon, result: dict) -> None:
    """Decorate timeline points with the parent's runtime name.

    Names are Registry state and ``recall.py`` takes only a ``Store``, so
    the id comes out of SQL and the name is attached here. A parent the
    Registry cannot resolve yields ``None``, not an error.
    """
    for entry in result.values():
        for point in entry.get("timeline", []):
            parent_id = point.get("parent_id")
            if parent_id is None:
                continue
            try:
                point["parent_name"] = daemon.registry.get(parent_id).name
            except Exception:
                point["parent_name"] = None


@method("recall_read")
async def _recall_read(daemon, params: dict) -> dict:
    """Explain one point of a recall timeline.

    A job segment reads its transcript back through the same
    ``open_source`` route as ``_read_transcript`` above, so a harness
    whose transcript is a database answers as well as one writing a
    file. A gap segment is the only place in the feature that forks
    ``git log``, which is why it is a separate call: the caller has
    looked at a gap and decided the fork is worth it.
    """
    from theater.daemon.recall_read import read_segment

    segment_id = _require(params, "segment_id")
    caller_cwd = params.get("caller_cwd") or "."
    return await read_segment(
        segment_id,
        store=daemon.store,
        registry=daemon.registry,
        cwd=caller_cwd,
        observer=daemon.observer,
    )
