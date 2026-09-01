"""Send RPC handler: preflight gates, pane identity, refusal, and delivery."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import NoReturn

from theater.constants.daemon import BUS_KIND_SEND_REFUSED, SEND_SUPERSEDED_ERROR_CODE

# Definition re-exported by the methods facade; runtime reads the facade for legacy patches.
from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS as SEND_CLAIM_TTL  # noqa: F401
from theater.daemon.harness_detect import (
    PaneHarnessVerdict,
    compare_detected_harness,
    detect_harness,
)
from theater.daemon.rpc.params import (
    _prompt_with_response_format,
    _require,
    _serialized_response_format,
)
from theater.daemon.rpc.router import method
from theater.harness import HARNESSES, normalize
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind
from theater.models import (
    AwaitingDecision,
    Busy,
    HumanPresent,
    JobState,
    NotAddressable,
    StaleTarget,
    Tier,
    TranscriptIdentityLost,
    TranscriptUntrusted,
    now,
)
from theater.provenance import is_trusted_provenance
from theater.tmux import client as tmux
from theater.tmux.presence import human_present
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    transcript_identity_recovery_message,
)


def _send_claim_ttl() -> float:
    from theater.daemon import methods as _facade

    return _facade.SEND_CLAIM_TTL


logger = logging.getLogger(__name__)


def _transcript_identity_lost(daemon, pid: str) -> bool:
    checker = getattr(daemon.observer, "transcript_identity_lost", None)
    return bool(checker(pid)) if callable(checker) else False


def _refuse_send(
    daemon, exc: Exception, *, reason: str, caller_id: str, target_id: str
) -> NoReturn:
    """Record a send that never became a job, then raise it.

    Counted by `Store.refusal_counts`. Kept as one bus kind with a `reason`
    rather than one kind per refusal, so a reader can subscribe to all of them
    without knowing the list.
    """
    daemon.store.bus_append(
        BUS_KIND_SEND_REFUSED,
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

    Fails open when tmux itself errors.
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
    an unrecoverable cost. This gate captures a fresh screen reading and
    refuses only when the kind is `APPROVAL` or `TRUST` at `HIGH` confidence.

    The `high` requirement is the safety margin. A false refusal makes a
    healthy pane permanently unreachable, so only a marker verified against a
    real captured screen may block a caller.

    Fails open, like `_check_pane_identity`.
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

    # There must be no await from this snapshot through reservation. Completion
    # consumes the oldest running job, so a replacement must close every expired
    # prompt-bearing predecessor before it creates its own reservation.
    stale = now() - _send_claim_ttl()
    running_prompt_jobs = [
        job for job in daemon.store.running_jobs_for_target(target_id) if job.prompt
    ]
    expired_jobs = [job for job in running_prompt_jobs if job.created_at <= stale]

    for job in expired_jobs:
        daemon.jobs.finish(
            job.handle,
            state=JobState.CRASHED,
            result=(
                f"Send to participant {target_id!r} was superseded after its delivery claim "
                "expired; it cannot receive the response to the newer prompt. Await the "
                "newer send handle instead."
            ),
            error_code=SEND_SUPERSEDED_ERROR_CODE,
        )

    if any(job.created_at > stale for job in running_prompt_jobs):
        refuse(
            Busy(f"participant {target_id!r} has a running send job"),
            reason="busy",
        )

    # Reserve before typing: the stale/fresh classification, closure, busy
    # refusal, and this create must not be separated by an await.
    handle = f"{target_id}#{daemon._next_send_seq()}"
    daemon.jobs.create(
        handle=handle,
        caller_id=caller_id,
        target_id=target_id,
        kind="send",
        prompt=prompt,
        cwd=target.cwd,
        response_format=response_format,
    )
    try:
        await tmux.deliver_text(target.tmux_pane, prompt)
    except Exception as exc:
        # Nothing was delivered, so nothing will ever answer. Close the job.
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
