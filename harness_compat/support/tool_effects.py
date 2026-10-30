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

from harness_compat.contract import MockReply, MockToolCall, RecordedRequest
from harness_compat.support.live_rig import (
    PROBE_SERVER,
    PROBE_TOOL,
    LiveRig,
    inconclusive,
    violation,
)
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
    edit = live.compat.native_edit
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
    mark = _mark(live)

    async def not_gated() -> None:
        check_tool_fit(live, prompt, effect.call)
        if not effect.happened() and (await live.reading(participant)).kind is ScreenKind.APPROVAL:
            shown = "\n".join((await live.screen(participant)).splitlines()[-8:])
            violation(
                f"the tool is waiting for approval although the approval policy allows it:\n{shown}"
            )

    job = await live.send(participant, prompt, guard=not_gated)

    check_tool_fit(live, prompt, effect.call)
    assert job.state == "done", job.error
    assert final in str(job.result)
    assert effect.happened(), "the tool call finished without leaving its effect"
    assert _received(live, mark), "the model never received the tool result"


async def expect_gated(live: LiveRig, participant: str, effect: Effect) -> None:
    prompt = "Use the tool"
    live.queue(effect.reply)
    live.queue(MockReply("the gated call must not reach this reply"))
    mark = _mark(live)
    handle = await live.submit(participant, prompt)

    async def still_waiting() -> None:
        check_tool_fit(live, prompt, effect.call)
        job = (await live.client.jobs.get(handle)).value
        if job.state != "running":
            violation(f"the turn ended ({job.state}) before its tool met an approval: {job.error}")

    await live.expect_screen(participant, ScreenKind.APPROVAL, guard=still_waiting)

    check_tool_fit(live, prompt, effect.call)
    await asyncio.sleep(GATE_SETTLE_SECONDS)
    assert not effect.happened(), "the tool ran while its approval prompt was open"
    assert not _received(live, mark), (
        "a result reached the model while the tool was awaiting approval"
    )


def _mark(live: LiveRig) -> tuple[int, set[str | None]]:
    """Where a turn starts, and the tool results the model already holds from earlier turns."""
    held = live.mock.requests
    return len(held), {request.last_tool_result for request in held}


def _received(live: LiveRig, mark: tuple[int, set[str | None]]) -> bool:
    """Whether a tool result new since ``mark`` reached the model; a request repeats its history."""
    seen, known = mark
    return bool(
        {request.last_tool_result for request in live.mock.requests[seen:]} - known - {None}
    )


def check_tool_fit(live: LiveRig, prompt: str, call: MockToolCall) -> None:
    """Before any outcome is judged: the scripted call must fit the tool the CLI offered.

    Theater does not choose a CLI's tool schemas, so a misfit means the plugin's recipe is stale:
    nothing is proven about the CLI, and the run is inconclusive rather than red. A turn the CLI
    never took has no offer to compare, so its own outcome is the evidence.
    """
    if not (requests := live.turns(prompt)):
        return
    problem = _misfit(requests[0], call)
    if problem is not None:
        inconclusive(
            f"the plugin's recipe for {call.name!r} does not fit the CLI's tool: {problem}"
        )


def _misfit(request: RecordedRequest, call: MockToolCall) -> str | None:
    if call.name not in request.tools:
        return "the model was not offered it"
    shape = request.tool_shapes.get(call.name)
    if shape is None:
        return None  # a freeform (grammar) tool declares no arguments to compare against
    if isinstance(call.arguments, str):
        return f"a freeform payload was scripted for a tool that declares {shape.properties}"
    given = set(call.arguments)
    unknown = sorted(given - set(shape.properties)) if shape.closed else []
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
    offered, name = await _offered(live, participant, server, tool)
    if name is None:
        violation(f"the model was never offered exactly one tool for {server}.{tool}: {offered}")
    declared = live.compat.mcp_call
    if declared is None:
        return MockToolCall(name, arguments)
    return MockToolCall(name, declared.render(server=server, tool=tool, arguments=arguments))


async def _offered(
    live: LiveRig, participant: str, server: str, tool: str
) -> tuple[tuple[str, ...], str | None]:
    """The offered tools and the name reaching ``tool``, retried as MCP servers may connect late.

    A CLI that offers MCP tools only after a search is made to run it first; the request that
    follows the search is the one that offers what it loaded.
    """
    discovery = live.compat.mcp_discovery
    for attempt in range(_OFFER_ATTEMPTS):
        prompt = f"Warm up {attempt}"
        if discovery is not None:
            # A CLI discards a call whose id an earlier one used, and every reply numbers from 0.
            search = MockToolCall(
                discovery.name,
                discovery.render(server=server, tool=tool),
                call_id=f"toolu_search_{uuid.uuid4().hex[:8]}",
            )
            live.queue(MockReply("loading", tool_calls=(search,)))
        live.queue(MockReply("warm"))
        seen = len(live.mock.requests)
        job = await live.send(participant, prompt)
        assert job.state == "done", job.error
        request = [r for r in live.mock.requests[seen:] if r.tools][-1]
        name = _reaching(live, request.tools, tool)
        if name is not None:
            return request.tools, name
        await asyncio.sleep(_OFFER_RETRY_SECONDS)
    return request.tools, None


def _reaching(live: LiveRig, offered: tuple[str, ...], tool: str) -> str | None:
    """The tool the model calls to reach ``tool``: the declared runner, else a suffix match."""
    declared = live.compat.mcp_call
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
    "check_tool_fit",
    "edit_effect",
    "expect_gated",
    "expect_runs",
    "mcp_effect",
    "refusal_effect",
]
