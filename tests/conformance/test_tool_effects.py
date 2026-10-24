"""A scripted call must fit the tool the CLI offered, or it proves nothing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.harness_compat.contract import MockToolCall, RecordedRequest, ToolShape
from tests.support.tool_effects import Effect, _misfit, expect_gated, expect_runs
from theater.harness.contracts.observation import ScreenKind


def _request(*shapes: tuple[str, ToolShape]) -> RecordedRequest:
    return RecordedRequest(
        "POST",
        "/v1/messages",
        True,
        (),
        True,
        tools=(*(name for name, _ in shapes), "apply_patch"),
        tool_shapes=dict(shapes),
    )


OPEN = ToolShape(("content", "path"), ("path",))
CLOSED = ToolShape(("content", "path"), ("path",), closed=True)


@pytest.mark.parametrize(
    ("shape", "arguments", "problem"),
    [
        pytest.param(OPEN, {"path": "a", "content": "b"}, None, id="fits"),
        pytest.param(
            OPEN, {"path": "a", "filePath": "a"}, None, id="extra-argument-on-an-open-schema"
        ),
        pytest.param(OPEN, {"file_path": "a"}, "missing required ['path']", id="missing-required"),
        pytest.param(
            CLOSED, {"path": "a", "filePath": "a"}, "unknown arguments ['filePath']", id="closed"
        ),
    ],
)
def test_arguments_must_satisfy_the_declared_shape(
    shape: ToolShape, arguments: dict[str, str], problem: str | None
) -> None:
    found = _misfit(_request(("write", shape)), MockToolCall("write", arguments))

    assert (found is None) if problem is None else (found is not None and problem in found)


def test_a_tool_the_model_was_not_offered_is_a_misfit() -> None:
    assert (
        _misfit(_request(("write", OPEN)), MockToolCall("other", {}))
        == "the model was not offered it"
    )


def test_a_freeform_tool_declares_nothing_to_compare_against() -> None:
    assert (
        _misfit(_request(("write", OPEN)), MockToolCall("apply_patch", "*** Begin Patch")) is None
    )


def test_a_freeform_payload_for_a_tool_declaring_arguments_is_a_misfit() -> None:
    found = _misfit(_request(("write", OPEN)), MockToolCall("write", "not an object"))

    assert found is not None
    assert "freeform payload" in found


class _Live:
    """What the two oracles read: recorded turns, a send that has already ended, the screen."""

    def __init__(self, requests: list[RecordedRequest], job: Any) -> None:
        self._requests = requests
        self._job = job
        self.mock = SimpleNamespace(requests=tuple(requests))
        self.client = SimpleNamespace(
            controls=SimpleNamespace(send=self._accept), jobs=SimpleNamespace(get=self._get)
        )

    def queue(self, reply: object) -> None:
        """The scripted reply goes nowhere: the job below has already ended."""

    def turns(self, prompt: str) -> list[RecordedRequest]:
        return self._requests

    async def send(self, participant: str, prompt: str, *, guard: Any) -> Any:
        await guard()
        return self._job

    async def reading(self, participant: str) -> Any:
        return SimpleNamespace(kind=ScreenKind.PROMPT)

    async def expect_screen(self, participant: str, kind: ScreenKind, *, guard: Any) -> None:
        await guard()
        pytest.fail("the screen never reached an approval")

    async def _accept(self, participant: str, prompt: str, *, idempotency_key: str) -> Any:
        return SimpleNamespace(value=SimpleNamespace(job_handle="j1"))

    async def _get(self, handle: str) -> Any:
        return SimpleNamespace(value=self._job)


def _effect(**arguments: str) -> Effect:
    return Effect(MockToolCall("write", arguments), lambda: False)


CRASHED = SimpleNamespace(state="crashed", error="the CLI refused the call", result=None)
ENDED = SimpleNamespace(state="done", error=None, result="finished without an approval")


@pytest.mark.parametrize("oracle", [expect_runs, expect_gated])
async def test_a_stale_recipe_is_inconclusive_even_when_the_turn_ended_early(oracle: Any) -> None:
    live = _Live([_request(("write", CLOSED))], CRASHED)

    with pytest.raises(pytest.skip.Exception, match="does not fit the CLI's tool"):
        await oracle(live, "p1", _effect(file_path="x"))


async def test_a_fitting_recipe_whose_turn_crashed_is_the_cli_failing() -> None:
    live = _Live([_request(("write", CLOSED))], CRASHED)

    with pytest.raises(AssertionError, match="the CLI refused the call"):
        await expect_runs(live, "p1", _effect(path="x"))


async def test_a_turn_that_ended_before_an_approval_is_a_violation_once_the_recipe_fits() -> None:
    live = _Live([_request(("write", CLOSED))], ENDED)

    with pytest.raises(pytest.fail.Exception, match="before its tool met an approval"):
        await expect_gated(live, "p1", _effect(path="x"))


async def test_a_turn_the_cli_never_took_reports_its_own_failure_not_a_recipe_problem() -> None:
    live = _Live([], CRASHED)

    with pytest.raises(AssertionError, match="the CLI refused the call"):
        await expect_runs(live, "p1", _effect(path="x"))
