"""Manifest-driven parent-to-child interruption RPC."""

from __future__ import annotations

from typing import NoReturn

from theater.constants.daemon import BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED
from theater.daemon.presence import access as presence_access
from theater.daemon.rpc import sending
from theater.daemon.rpc.params import _string_param
from theater.daemon.rpc.router import method
from theater.harness import HARNESSES, normalize
from theater.harness.contracts.runtime import RuntimeCapability
from theater.models import BadRequest, NotAddressable, NotYourChild, Status
from theater.tmux import client as tmux


def _refuse_interrupt(exc: Exception, *, reason: str) -> NoReturn:
    del reason
    raise exc


def _ensure_addressable(target) -> None:
    if not target.addressable:
        raise NotAddressable(
            f"participant {target.id!r} is not addressable "
            f"(tier={target.tier}, status={target.status})"
        )
    if not target.tmux_pane:
        raise NotAddressable(f"participant {target.id!r} has no pane to interrupt")


def _interrupt_plan(target):
    harness = HARNESSES.get(normalize(target.harness))
    controls = None if harness is None else getattr(harness, "controls", None)
    plan = None if controls is None else getattr(controls, "interrupt", None)
    if plan is None:
        raise BadRequest(
            f"harness {target.harness!r} does not declare an interrupt control; "
            "ask its plugin owner to add controls.interrupt"
        )
    return plan


@method("participant.interrupt")
async def _interrupt(daemon, params: dict) -> dict:
    target = daemon.registry.resolve(
        _string_param(params, "target", method_name="participant.interrupt")
    )
    caller_id = _string_param(params, "caller_id", method_name="participant.interrupt")
    target_id = target.id

    route = daemon.controls.route_for(target_id, RuntimeCapability.INTERRUPT)
    if route.native_wiring and not route.is_legacy:
        # The service owns the semantics: authorization first, then the
        # followup cancellation and interruption of the exact active
        # native turn. A persisted native binding without a live runtime
        # fails closed inside the service with no mutation.
        outcome = await daemon.controls.interrupt(target_id, caller_id=caller_id)
        result = {"id": target_id, "interrupted": outcome.interrupted}
        if outcome.reason is not None:
            result["reason"] = outcome.reason
        if outcome.cancelled_followups:
            result["cancelled_followups"] = list(outcome.cancelled_followups)
        if outcome.interrupted:
            daemon.store.bus_append(
                BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED,
                from_id=caller_id,
                to_id=target_id,
            )
        return result

    if target.id == caller_id:
        raise NotYourChild(f"refusing to interrupt {target_id!r}: that is you, not your child")
    if target.parent_id != caller_id:
        raise NotYourChild(
            f"refusing to interrupt {target_id!r}: its parent is "
            f"{target.parent_id!r}, not you ({caller_id!r})"
        )

    _ensure_addressable(target)
    if target.status is not Status.WORKING:
        return {"id": target_id, "interrupted": False, "reason": "already_not_working"}
    plan = _interrupt_plan(target)
    # Gate before the awaited identity check: no status mutation while protected.
    await presence_access.require_absent(daemon, target_id)
    await sending._check_pane_identity(daemon, target, _refuse_interrupt)
    target = daemon.registry.get(target_id)
    _ensure_addressable(target)
    if target.status is not Status.WORKING:
        return {"id": target_id, "interrupted": False, "reason": "already_not_working"}
    await presence_access.require_absent(daemon, target_id)
    refusal = await sending.copy_mode_refusal(target.tmux_pane)
    if refusal is not None:
        raise refusal
    # Recheck after the awaited copy-mode query, immediately before injection.
    await presence_access.require_absent(daemon, target_id)

    await tmux.deliver_keys(
        target.tmux_pane,
        plan.keys,
        inter_key_delay_seconds=plan.inter_key_delay_seconds,
    )
    daemon.store.bus_append(
        BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED,
        from_id=caller_id,
        to_id=target_id,
    )
    return {"id": target_id, "interrupted": True}
