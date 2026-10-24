"""Scripted tool calls with an observable effect, and the two outcomes an approval allows.

A call either runs, leaving its effect and its result in the next model request, or is gated:
the plugin's own classifier reads an approval prompt and nothing happens behind it. Before either
outcome is judged, the scripted call must fit the schema the CLI offered the tool with.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.harness_compat.contract import MockReply, MockToolCall, RecordedRequest
from tests.support.live_rig import PROBE_SERVER, PROBE_TOOL, LiveRig, violation
from theater.constants.harness import HARNESS_MCP_SERVER_NAME
from theater.harness.contracts.observation import ScreenKind

#: Theater's own MCP server and a tool of it that refuses an unknown participant.
THEATER_SERVER = HARNESS_MCP_SERVER_NAME
REFUSING_TOOL = "get_session_controls"
#: How long an open approval prompt must keep its effect from happening.
GATE_SETTLE_SECONDS = 3.0
_OFFER_ATTEMPTS = 3
_OFFER_RETRY_SECONDS = 3.0


@dataclass(frozen=True)
class Effect:
    """The model turn that calls a tool, and how to tell that the call took effect."""

    call: MockToolCall
    happened: Callable[[], bool]

    @property
    def reply(self) -> MockReply:
        return MockReply("calling", tool_calls=(self.call,))


def edit_effect(live: LiveRig) -> Effect:
    """The harness's own file-write tool, aimed at a scratch file inside the workdir."""
    edit = live.harness.compat.native_edit
    assert edit is not None
    nonce = f"EDIT-{uuid.uuid4().hex[:8]}"
    target = live.workdir.resolve() / f"{nonce}.txt"
    call = MockToolCall(edit.name, edit.render(path=str(target), nonce=nonce))
    return Effect(call, lambda: _contains(target, nonce))


async def mcp_effect(live: LiveRig, participant: str) -> Effect:
    """The probe sidecar's tool, which leaves a marker file when it runs."""
    label = f"MARK-{uuid.uuid4().hex[:8]}"
    call = await _mcp_call(live, participant, PROBE_SERVER, PROBE_TOOL, {"label": label})
    return Effect(call, (live.marker_dir / label).exists)


async def refusal_effect(live: LiveRig, participant: str) -> Effect:
    """A refusal from Theater's own MCP server: its text must reach the model."""
    target = f"no-such-{uuid.uuid4().hex[:8]}"
    call = await _mcp_call(live, participant, THEATER_SERVER, REFUSING_TOOL, {"target": target})

    def reached_the_model() -> bool:
        return any(target in (request.last_tool_result or "") for request in live.mock.requests)

    return Effect(call, reached_the_model)


async def expect_runs(live: LiveRig, participant: str, effect: Effect) -> None:
    final = f"DONE-{uuid.uuid4().hex[:8]}"
    prompt = f"Use the tool, then reply with {final}"
    live.queue(effect.reply)
    live.queue(MockReply(final))

    async def not_gated() -> None:
        if not effect.happened() and (await live.reading(participant)).kind is ScreenKind.APPROVAL:
            violation("the tool is waiting for approval although the approval policy allows it")

    job = await live.send(participant, prompt, guard=not_gated)

    _check_fit(live, prompt, effect)
    assert job.state == "done", job.error
    assert final in str(job.result)
    assert effect.happened(), "the tool call finished without leaving its effect"
    assert any(request.last_tool_result for request in live.mock.requests), (
        "the model never received the tool result"
    )


async def expect_gated(live: LiveRig, participant: str, effect: Effect) -> None:
    prompt = "Use the tool"
    live.queue(effect.reply)
    live.queue(MockReply("the gated call must not reach this reply"))
    await live.client.controls.send(participant, prompt, idempotency_key=uuid.uuid4().hex)

    await live.expect_screen(participant, ScreenKind.APPROVAL)

    _check_fit(live, prompt, effect)
    await asyncio.sleep(GATE_SETTLE_SECONDS)
    assert not effect.happened(), "the tool ran while its approval prompt was open"
    assert not any(request.last_tool_result for request in live.mock.requests), (
        "a result reached the model while the tool was awaiting approval"
    )


def _check_fit(live: LiveRig, prompt: str, effect: Effect) -> None:
    """The scripted call must use arguments the offered tool declares, or it proves nothing."""
    request = live.turns(prompt)[0]
    problem = _misfit(request, effect.call)
    if problem is not None:
        violation(
            f"the scripted call to {effect.call.name!r} does not fit the CLI's tool: {problem}"
        )


def _misfit(request: RecordedRequest, call: MockToolCall) -> str | None:
    if call.name not in request.tools:
        return "the model was not offered it"
    shape = request.tool_shapes.get(call.name)
    if shape is None or isinstance(call.arguments, str):
        return None  # a freeform (grammar) tool declares no arguments to compare against
    given = set(call.arguments)
    unknown = sorted(given - set(shape.properties)) if shape.properties else []
    missing = sorted(set(shape.required) - given)
    if unknown or missing:
        return (
            f"unknown arguments {unknown}, missing required {missing}, declared {shape.properties}"
        )
    return None


async def _mcp_call(
    live: LiveRig, participant: str, server: str, tool: str, arguments: Mapping[str, object]
) -> MockToolCall:
    """The call that makes this CLI's model invoke an MCP tool, by its own name or the runner."""
    offered, name = await _offered(live, participant, tool)
    if name is None:
        violation(f"the model was never offered exactly one tool for {server}.{tool}: {offered}")
    declared = live.harness.compat.mcp_call
    if declared is None:
        return MockToolCall(name, arguments)
    return MockToolCall(name, declared.render(server=server, tool=tool, arguments=arguments))


async def _offered(
    live: LiveRig, participant: str, tool: str
) -> tuple[tuple[str, ...], str | None]:
    """The offered tools and the name reaching ``tool``, retried as MCP servers may connect late."""
    for attempt in range(_OFFER_ATTEMPTS):
        prompt = f"Warm up {attempt}"
        live.queue(MockReply("warm"))
        job = await live.send(participant, prompt)
        assert job.state == "done", job.error
        (request,) = live.turns(prompt)
        name = _reaching(live, request.tools, tool)
        if name is not None:
            return request.tools, name
        await asyncio.sleep(_OFFER_RETRY_SECONDS)
    return request.tools, None


def _reaching(live: LiveRig, offered: tuple[str, ...], tool: str) -> str | None:
    """The tool the model calls to reach ``tool``: the declared runner, else a suffix match."""
    declared = live.harness.compat.mcp_call
    if declared is not None:
        return declared.name if declared.name in offered else None
    matches = [name for name in offered if name.endswith(tool)]
    return matches[0] if len(matches) == 1 else None


def _contains(path: Path, text: str) -> bool:
    try:
        return text in path.read_text(encoding="utf-8")
    except OSError:
        return False


__all__ = [
    "Effect",
    "edit_effect",
    "expect_gated",
    "expect_runs",
    "mcp_effect",
    "refusal_effect",
]
