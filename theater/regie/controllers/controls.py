"""Non-blocking participant controls for the régie.

Steer, queued followups, settings updates, capability inspection, and
interruption all run through :class:`ControlController` as background tasks,
the way kills already do. Two properties matter and are tested:

- **Per-operation connections.** Every request opens its own
  ``DaemonClient`` and closes it when done, so one slow control never
  occupies the polling client and never serializes another participant's
  control behind a shared socket lock.
- **Per-target coalescing.** A second identical action for the same
  participant while one is in flight is refused explicitly, rather than
  piling a duplicate mutation onto a session the daemon may already be
  servicing.

Support is never decided here. The daemon owns capability policy; the régie
forwards the action and shows whatever reason comes back — as a refused
receipt, an unavailable-capability entry in the controls report, or an error
notification. Nothing is inferred from harness names or wiring fields locally.

The formatting helpers (``describe_receipt``, ``describe_interrupt``,
``format_controls_report``) are Textual-free and defensive by design: the
daemon response vocabulary is additive and versioned separately, so unknown
delivery words are shown verbatim and missing fields render as absent rather
than guessed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from theater.client import DaemonClient
from theater.constants.regie import (
    REGIE_CONTROLS_REPORT_LINE_MAX,
    REGIE_CONTROLS_REPORT_MAX_LINES,
)

logger = logging.getLogger("theater.regie.controls")

#: The five régie actions, used as coalescing keys and notification labels.
ACTION_STEER = "steer"
ACTION_QUEUE = "queue"
ACTION_SETTINGS = "settings"
ACTION_INTERRUPT = "interrupt"
ACTION_INSPECT = "inspect"

RPC_STEER = "participant.steer"
RPC_QUEUE_FOLLOWUP = "participant.queue_followup"
RPC_SETTINGS_UPDATE = "participant.settings.update"
RPC_CONTROLS = "participant.controls"
RPC_INTERRUPT = "participant.interrupt"

#: The operator token ``participant.kill`` already accepts for the local
#: CLI/régie caller. Reused verbatim so no second operator concept exists.
OPERATOR_CALLER_ID = "cli"

#: The notification severity vocabulary the app's `notify` accepts.
type ControlSeverity = Literal["information", "warning", "error"]

#: Receipt delivery words the daemon may send; anything else is shown as-is.
_ACCEPTED_DELIVERY = {"accepted", "delivered", "dispatched"}
_REJECTED_DELIVERY = {"rejected", "refused"}

#: Shared suffix for every outcome the daemon could not confirm. The request
#: may still have landed — the persisted UNKNOWN result may never resolve —
#: so a blind retry could duplicate the very action the operator just sent.
_UNKNOWN_OUTCOME_SUFFIX = " — do not retry blindly; the result may remain unknowable"


@dataclass(frozen=True)
class ControlOutcome:
    """Outcome of one participant control request."""

    participant_id: str
    action: str
    ok: bool
    result: Mapping[str, Any] | None = None
    error: str | None = None


type ControlCallback = Callable[[ControlOutcome], Awaitable[None]]


@dataclass(frozen=True)
class _ControlRequest:
    """One queued daemon call: the method, its params, and its action label."""

    action: str
    method: str
    params: dict[str, Any]


class ControlController:
    """Run coalesced participant controls on per-operation connections."""

    def __init__(self, client_factory: Callable[..., DaemonClient]) -> None:
        self._factory = client_factory
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._closed = False

    @property
    def in_flight(self) -> frozenset[tuple[str, str]]:
        """(participant_id, action) pairs with a request still running."""
        return frozenset(self._tasks)

    def steer(
        self,
        participant_id: str,
        prompt: str,
        on_done: ControlCallback | None = None,
    ) -> bool:
        """Amend exactly the current Theater job for *participant_id*."""
        return self._request(
            participant_id,
            _ControlRequest(
                action=ACTION_STEER,
                method=RPC_STEER,
                params={
                    "target": participant_id,
                    "prompt": prompt,
                    "caller_id": OPERATOR_CALLER_ID,
                },
            ),
            on_done,
        )

    def queue_followup(
        self,
        participant_id: str,
        prompt: str,
        on_done: ControlCallback | None = None,
    ) -> bool:
        """Queue a followup that returns a new awaitable send-job handle."""
        return self._request(
            participant_id,
            _ControlRequest(
                action=ACTION_QUEUE,
                method=RPC_QUEUE_FOLLOWUP,
                params={
                    "target": participant_id,
                    "prompt": prompt,
                    "caller_id": OPERATOR_CALLER_ID,
                },
            ),
            on_done,
        )

    def update_settings(
        self,
        participant_id: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        on_done: ControlCallback | None = None,
    ) -> bool:
        """Apply an idle-only model/reasoning change to *participant_id*."""
        params: dict[str, Any] = {"target": participant_id, "caller_id": OPERATOR_CALLER_ID}
        if model:
            params["model"] = model
        if reasoning_effort:
            params["reasoning_effort"] = reasoning_effort
        return self._request(
            participant_id,
            _ControlRequest(
                action=ACTION_SETTINGS,
                method=RPC_SETTINGS_UPDATE,
                params=params,
            ),
            on_done,
        )

    def interrupt(self, participant_id: str, on_done: ControlCallback | None = None) -> bool:
        """Cancel the active turn and every undelivered followup."""
        return self._request(
            participant_id,
            _ControlRequest(
                action=ACTION_INTERRUPT,
                method=RPC_INTERRUPT,
                params={"target": participant_id, "caller_id": OPERATOR_CALLER_ID},
            ),
            on_done,
        )

    def inspect(self, participant_id: str, on_done: ControlCallback | None = None) -> bool:
        """Fetch effective capabilities, health, settings, and queued handles."""
        return self._request(
            participant_id,
            _ControlRequest(
                action=ACTION_INSPECT,
                method=RPC_CONTROLS,
                params={"target": participant_id, "caller_id": OPERATOR_CALLER_ID},
            ),
            on_done,
        )

    def _request(
        self,
        participant_id: str,
        request: _ControlRequest,
        on_done: ControlCallback | None,
    ) -> bool:
        """Start one control unless its (participant, action) pair is running."""
        if self._closed:
            return False
        key = (participant_id, request.action)
        if key in self._tasks:
            return False
        task = asyncio.create_task(
            self._run(participant_id, request, on_done),
            name=f"regie-control-{request.action}-{participant_id}",
        )
        self._tasks[key] = task

        def _forget(task: asyncio.Task[None], key: tuple[str, str] = key) -> None:
            if self._tasks.get(key) is task:
                del self._tasks[key]

        task.add_done_callback(_forget)
        return True

    async def _run(
        self,
        participant_id: str,
        request: _ControlRequest,
        on_done: ControlCallback | None,
    ) -> None:
        # One client per operation: a connection stuck on participant A must
        # not hold participant B's control or a poll behind its socket lock.
        client: DaemonClient | None = None
        try:
            client = self._factory()
            await client.connect()
            answer = await client.call(request.method, **request.params)
            result = answer if isinstance(answer, Mapping) else None
            outcome = ControlOutcome(
                participant_id=participant_id,
                action=request.action,
                ok=True,
                result=result,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome = ControlOutcome(
                participant_id=participant_id,
                action=request.action,
                ok=False,
                error=str(exc),
            )
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.aclose()
        if on_done is None:
            return
        try:
            await on_done(outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "control completion handling failed for %s on %s",
                request.action,
                participant_id,
            )

    async def aclose(self) -> None:
        """Cancel and drain every in-flight control."""
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()


# ---- daemon-derived presentation (Textual-free) ---------------------------


def _bounded(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) > REGIE_CONTROLS_REPORT_LINE_MAX:
        return text[: REGIE_CONTROLS_REPORT_LINE_MAX - 1] + "…"
    return text


def _delivery_of(result: Mapping[str, Any]) -> str:
    """The daemon's own delivery word, or "" when it sent none."""
    for key in ("delivery", "status", "phase"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _receipt_detail(result: Mapping[str, Any]) -> str:
    """Reason code, human detail, and phase from a receipt, verbatim.

    Joined and deduplicated so a receipt that repeats itself does not render
    the same fact three times.
    """
    parts: list[str] = []
    for key in ("reason", "detail", "phase"):
        value = result.get(key)
        if not isinstance(value, str):
            continue
        part = " ".join(value.split())
        if part and part not in parts:
            parts.append(part)
    return f" ({'; '.join(parts)})" if parts else ""


def _error_detail(result: Mapping[str, Any]) -> str:
    """Error code and human error text from a refusal, verbatim."""
    parts: list[str] = []
    for key in ("error_code", "error"):
        value = result.get(key)
        if not isinstance(value, str):
            continue
        part = " ".join(value.split())
        if part and part not in parts:
            parts.append(part)
    return f" ({'; '.join(parts)})" if parts else ""


def _handle_of(result: Mapping[str, Any]) -> str:
    """The send-job handle a queued followup reserved, if the daemon sent one."""
    for key in ("handle", "job_handle"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def describe_receipt(action: str, result: object) -> tuple[str, ControlSeverity]:
    """(message, severity) for one steer/queue receipt.

    ``participant.steer`` answers with an additive ``delivery`` word —
    ``"accepted"`` or ``"unknown"`` — plus phase/reason details, and
    ``participant.queue_followup`` with a send-job handle. A malformed or
    missing answer is never read as success: without a handle or a delivery
    word the outcome is unknown, and an unknown outcome is a warning that
    must not be retried blindly — the persisted result may stay UNKNOWN.
    An unfamiliar delivery word is shown verbatim rather than flattened
    into "accepted".
    """
    unknown = f"{action} delivery unknown{_UNKNOWN_OUTCOME_SUFFIX}"
    if not isinstance(result, Mapping):
        return unknown, "warning"
    detail = _receipt_detail(result)
    unknown = f"{unknown}{detail}"
    delivery = _delivery_of(result)
    if delivery in _REJECTED_DELIVERY:
        return f"{action} rejected{detail}", "error"
    if delivery == "unknown":
        return unknown, "warning"
    if delivery == "pending":
        return f"{action} pending — delivery not confirmed yet{detail}", "warning"
    if action == ACTION_QUEUE:
        handle = _handle_of(result)
        if handle:
            return f"followup queued as {handle}{detail}", "information"
    if delivery in _ACCEPTED_DELIVERY:
        return f"{action} accepted{detail}", "information"
    if not delivery:
        return unknown, "warning"
    return f"{action} {delivery}{detail}", "information"


def describe_settings(result: object) -> tuple[str, ControlSeverity]:
    """(message, severity) for a settings update from its ``applied`` answer.

    ``applied`` is three-valued on purpose: ``True`` means the daemon applied
    the change, ``False`` is a definitive refusal, and ``None`` means the
    delivery or application outcome is unknown — the persisted UNKNOWN
    result may never resolve, so it is a warning, not a success and not a
    promise that anything will reconcile. Rendering ``False`` or ``None``
    as success would hide a refusal or an unresolved mutation.
    """
    detail = _error_detail(result) if isinstance(result, Mapping) else ""
    applied = result.get("applied") if isinstance(result, Mapping) else None
    if applied is True:
        return "settings updated", "information"
    if applied is False:
        return f"settings update refused{detail}", "error"
    return f"settings update outcome unknown{_UNKNOWN_OUTCOME_SUFFIX}{detail}", "warning"


def describe_interrupt(result: object) -> tuple[str, ControlSeverity]:
    """(message, severity) for an interrupt using the existing RPC's reply.

    The daemon answers ``{"interrupted": true}`` or ``{"interrupted": false,
    "reason": ...}``. ``already_idle``/``already_not_working`` are terminal
    informational no-ops. ``delivery_unknown`` means the transmission or ack
    is uncertain — an explicit warning, not a no-op, and not something to
    retry blindly. Any other refusal reason, a missing ``interrupted``
    field, or a malformed answer is likewise never shown as success.
    """
    unknown = f"interrupt delivery unknown{_UNKNOWN_OUTCOME_SUFFIX}"
    if not isinstance(result, Mapping):
        return unknown, "warning"
    interrupted = result.get("interrupted")
    if interrupted is True:
        return "interrupted", "information"
    if interrupted is False:
        reason = result.get("reason")
        why = f" — {reason}" if isinstance(reason, str) and reason else ""
        if reason in ("already_idle", "already_not_working"):
            return f"nothing to interrupt{why}", "information"
        if reason == "delivery_unknown":
            return unknown, "warning"
        return f"interrupt not performed{why}", "warning"
    return unknown, "warning"


def _capability_state(cap: object) -> tuple[bool, str | None, str | None]:
    """(available, reason code, human detail) from one capability entry.

    Entries are ``{"available": bool, "reason"?: str, "detail"?: str}`` —
    ``reason`` is the daemon's reason code and ``detail`` its human
    explanation. Neither is interpreted here; both are shown verbatim.
    """
    if isinstance(cap, bool):
        return cap, None, None
    if isinstance(cap, Mapping):
        available = cap.get("available", cap.get("supported", True))
        reason = cap.get("reason")
        detail = cap.get("detail")
        return (
            bool(available),
            reason if isinstance(reason, str) and reason else None,
            detail if isinstance(detail, str) and detail else None,
        )
    if cap is None:
        return True, None, None
    return True, str(cap), None


def _capability_lines(capabilities: object) -> list[str]:
    if not isinstance(capabilities, Mapping):
        return []
    lines: list[str] = []
    for name, cap in capabilities.items():
        available, reason, detail = _capability_state(cap)
        if available:
            lines.append(f"{name}: available")
            continue
        parts = [part for part in (reason, detail) if part]
        if not parts:
            lines.append(f"{name}: unavailable — no reason given")
            continue
        rendered = (
            f"{_bounded(parts[0])}"
            if len(parts) == 1
            else (f"{_bounded(parts[0])} ({_bounded(parts[1])})")
        )
        lines.append(f"{name}: unavailable — {rendered}")
    return lines


def _queued_handle(item: object) -> str:
    """One queued-followup entry: a handle string, or its handle field."""
    if isinstance(item, Mapping):
        handle = item.get("handle")
        if isinstance(handle, str) and handle:
            return handle
    return str(item) if item is not None else ""


def _health_lines(health: object) -> list[str]:
    """Render the health section: ``{"connection": str, "diagnostics": list}``.

    A plain string is rendered as-is for older additive shapes; an empty
    diagnostics list is simply absent rather than an empty line.
    """
    if isinstance(health, str) and health:
        return [f"health: {_bounded(health)}"]
    if not isinstance(health, Mapping):
        return []
    lines: list[str] = []
    connection = health.get("connection")
    if isinstance(connection, str) and connection:
        lines.append(f"health: connection={_bounded(connection)}")
    diagnostics = health.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        shown = "; ".join(_bounded(item) for item in diagnostics[:5])
        lines.append(f"diagnostics: {shown}")
    return lines


def format_controls_report(result: Mapping[str, Any] | None) -> str:
    """Render a ``participant.controls`` answer as bounded plain text.

    Every line is a daemon-derived fact; a section the daemon did not send is
    simply absent, and nothing about support is inferred locally.
    """
    lines: list[str] = []
    if isinstance(result, Mapping):
        for key in ("wiring",):
            value = result.get(key)
            if isinstance(value, str) and value:
                lines.append(f"{key}: {_bounded(value)}")
        lines.extend(_health_lines(result.get("health")))
        lines.extend(_capability_lines(result.get("capabilities")))
        settings = result.get("settings")
        if isinstance(settings, Mapping) and settings:
            shown = ", ".join(f"{key}={_bounded(value)}" for key, value in settings.items())
            lines.append(f"settings: {shown}")
        turn = result.get("active_turn")
        if isinstance(turn, Mapping) and turn:
            identifier = turn.get("native_turn_id") or turn.get("id")
            lines.append("active turn: " + (_bounded(identifier) if identifier else "yes"))
        elif isinstance(turn, str) and turn:
            lines.append(f"active turn: {_bounded(turn)}")
        queued = result.get("queued")
        if queued is None:
            queued = result.get("queued_handles")
        if isinstance(queued, list):
            handles = ", ".join(_bounded(_queued_handle(item)) for item in queued[:5])
            suffix = f" ({handles})" if handles else ""
            lines.append(f"queued followups: {len(queued)}{suffix}")
    if not lines:
        return "the daemon reported no controls"
    return "\n".join(_bounded(line) for line in lines[:REGIE_CONTROLS_REPORT_MAX_LINES])
