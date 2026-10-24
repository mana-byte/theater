"""A scripted call must fit the tool the CLI offered, or it proves nothing."""

from __future__ import annotations

import pytest

from scripts.harness_compat.contract import MockToolCall, RecordedRequest, ToolShape
from tests.support.tool_effects import _misfit


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
