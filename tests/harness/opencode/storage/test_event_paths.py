"""OpenCode tool-call paths and their relativisation."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.opencode_recorder import attached, tool
from tests.support.opencode_recorder import rec as shared_rec
from theater.harness import EventKind
from theater.harness.base import EventPath
from theater.harness.builtin.plugins.opencode import _paths_from_tool, _relativise

rec = shared_rec


def _tool_call_events(events: list) -> list:
    return [e for e in events if e.kind is EventKind.TOOL_CALL]


@pytest.mark.parametrize(
    ("tool", "state", "expected"),
    [
        pytest.param(
            "write",
            {"input": {"filePath": "src/app.py", "content": "x = 1"}},
            (EventPath(path="src/app.py", mode="write"),),
            id="write",
        ),
        pytest.param(
            "edit",
            {"input": {"filePath": "src/app.py", "oldString": "a", "newString": "b"}},
            (EventPath(path="src/app.py", mode="write"),),
            id="edit",
        ),
        pytest.param(
            "read",
            {"input": {"filePath": "README.md", "offset": 1, "limit": 100}},
            (EventPath(path="README.md", mode="read"),),
            id="read",
        ),
        pytest.param(
            "webfetch",
            {"input": {"url": "https://example.com", "format": "markdown"}},
            (),
            id="webfetch_has_no_paths",
        ),
        pytest.param(
            "write",
            {"status": "pending", "input": {}},
            (),
            id="pending_carries_no_input_yet",
        ),
        pytest.param("write", {"input": {"content": "x"}}, (), id="missing_filepath"),
        pytest.param("write", {"input": {"filePath": 42}}, (), id="non_string_filepath"),
    ],
)
def test_tool_input_yields_its_paths(tool, state, expected):
    assert _paths_from_tool(tool, state, "/repo") == expected


@pytest.mark.parametrize(
    ("path", "cwd", "expected"),
    [
        pytest.param("/repo/src/app.py", "/repo", "src/app.py", id="absolute_inside_repo"),
        pytest.param("src/app.py", "/repo", "src/app.py", id="relative_unchanged"),
        pytest.param("/other/src/app.py", "/repo", None, id="absolute_outside_repo"),
        # No cwd to relativise against: keeping the path would leak a home directory.
        pytest.param("/home/alice/repo/src/app.py", None, None, id="absolute_without_cwd"),
        pytest.param("", "/repo", None, id="empty"),
    ],
)
def test_relativise(path, cwd, expected):
    assert _relativise(path, cwd) == expected


@pytest.mark.parametrize("name,mode", [("write", "write"), ("edit", "write"), ("read", "read")])
def test_native_file_tools_carry_their_paths_in_live_and_history(rec, workdir, name, mode):
    src = attached(rec, workdir)
    part = tool("call_one", "completed", str(workdir / "src/app.py"), "ok")
    part["name"] = name
    rec.step("msg_a1", rec.tick(), [part], finish="tool-calls", times={"completed": rec.tick()})

    live = _tool_call_events(asyncio.run(src.read()).events)
    history = _tool_call_events(asyncio.run(src.history(last_n=0)).events)
    assert len(live) == len(history) == 1
    assert live[0].paths == history[0].paths == (EventPath(path="src/app.py", mode=mode),)


def test_two_tool_calls_in_one_message_each_carry_paths(rec, workdir):
    src = attached(rec, workdir)
    write = tool("call_write", "completed", "src/new.py", "ok")
    write["name"] = "write"
    read = tool("call_read", "completed", "src/old.py", "contents")
    rec.step("msg_a1", rec.tick(), [write, read])

    calls = _tool_call_events(asyncio.run(src.read()).events)
    assert [event.paths for event in calls] == [
        (EventPath(path="src/new.py", mode="write"),),
        (EventPath(path="src/old.py", mode="read"),),
    ]
