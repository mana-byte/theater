"""Scripted tool calls with an observable side effect, and the two outcomes an approval allows.

A call either runs, leaving its effect on disk and its result in the next model request, or is
gated: the plugin's own classifier reads an approval prompt and nothing happens behind it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.harness_compat.contract import MockReply, MockToolCall
from tests.support.live_rig import PROBE_SERVER, PROBE_TOOL, LiveRig, violation
from theater.harness.contracts.observation import ScreenKind

#: How long an open approval prompt must keep its effect from happening.
GATE_SETTLE_SECONDS = 3.0
_OFFER_ATTEMPTS = 3
_OFFER_RETRY_SECONDS = 3.0


@dataclass(frozen=True)
class Effect:
    """The model turn that calls a tool, and how to tell on disk that the call ran."""

    reply: MockReply
    happened: Callable[[], bool]


def edit_effect(live: LiveRig) -> Effect:
    """The harness's own file-write tool, aimed at a scratch file inside the workdir."""
    edit = live.harness.compat.native_edit
    assert edit is not None
    nonce = f"EDIT-{uuid.uuid4().hex[:8]}"
    target = live.workdir.resolve() / f"{nonce}.txt"
    arguments = edit.render(path=str(target), nonce=nonce)
    if isinstance(arguments, str):
        raise NotImplementedError("freeform native_edit payloads need mock support first")
    call = MockToolCall(edit.name, arguments)
    return Effect(MockReply("editing", tool_calls=(call,)), lambda: _contains(target, nonce))


async def mcp_effect(live: LiveRig, participant: str) -> Effect:
    """The probe sidecar's tool, called however this CLI's model must call an MCP tool."""
    label = f"MARK-{uuid.uuid4().hex[:8]}"
    offered, name = await _offered_probe(live, participant)
    if name is None:
        violation(f"the model was never offered exactly one tool for the probe sidecar: {offered}")
    payload: Mapping[str, object] = {"label": label}
    declared = live.harness.compat.mcp_call
    if declared is not None:
        rendered = declared.render(server=PROBE_SERVER, tool=PROBE_TOOL, arguments=payload)
        if isinstance(rendered, str):
            raise NotImplementedError("freeform mcp_call payloads need mock support first")
        payload = rendered
    marker = live.marker_dir / label
    return Effect(MockReply("calling", tool_calls=(MockToolCall(name, payload),)), marker.exists)


async def expect_runs(live: LiveRig, participant: str, effect: Effect) -> None:
    final = f"DONE-{uuid.uuid4().hex[:8]}"
    live.queue(effect.reply)
    live.queue(MockReply(final))

    async def not_gated() -> None:
        if not effect.happened() and (await live.reading(participant)).kind is ScreenKind.APPROVAL:
            violation("the tool is waiting for approval although the approval policy allows it")

    job = await live.send(participant, f"Use the tool, then reply with {final}", guard=not_gated)

    assert job.state == "done", job.error
    assert final in str(job.result)
    assert effect.happened(), "the tool call finished without leaving its effect"
    assert any(request.last_tool_result for request in live.mock.requests), (
        "the model never received the tool result"
    )


async def expect_gated(live: LiveRig, participant: str, effect: Effect) -> None:
    live.queue(effect.reply)
    live.queue(MockReply("the gated call must not reach this reply"))
    await live.client.controls.send(participant, "Use the tool", idempotency_key=uuid.uuid4().hex)

    await live.expect_screen(participant, ScreenKind.APPROVAL)

    await asyncio.sleep(GATE_SETTLE_SECONDS)
    assert not effect.happened(), "the tool ran while its approval prompt was open"
    assert not any(request.last_tool_result for request in live.mock.requests), (
        "a result reached the model while the tool was awaiting approval"
    )


async def _offered_probe(live: LiveRig, participant: str) -> tuple[tuple[str, ...], str | None]:
    """The offered tools and the one reaching the probe, retried as MCP servers may connect late."""
    for attempt in range(_OFFER_ATTEMPTS):
        prompt = f"Warm up {attempt}"
        live.queue(MockReply("warm"))
        job = await live.send(participant, prompt)
        assert job.state == "done", job.error
        (request,) = [
            item for item in live.mock.requests if item.last_user == prompt and item.tools
        ]
        name = _probe_tool(live, request.tools)
        if name is not None:
            return request.tools, name
        await asyncio.sleep(_OFFER_RETRY_SECONDS)
    return request.tools, None


def _probe_tool(live: LiveRig, offered: tuple[str, ...]) -> str | None:
    """The tool the model calls to reach the probe: the declared runner, else a suffix match."""
    declared = live.harness.compat.mcp_call
    if declared is not None:
        return declared.name if declared.name in offered else None
    matches = [name for name in offered if name.endswith(PROBE_TOOL)]
    return matches[0] if len(matches) == 1 else None


def _contains(path: Path, text: str) -> bool:
    try:
        return text in path.read_text(encoding="utf-8")
    except OSError:
        return False


__all__ = ["Effect", "edit_effect", "expect_gated", "expect_runs", "mcp_effect"]
