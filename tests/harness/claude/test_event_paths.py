"""EventPath extraction from Claude Code transcripts."""

from __future__ import annotations

import json

import pytest

from tests.support.shipped import ClaudeCodeObserver
from theater.harness.base import EventKind


def _tool_use_record(
    tool_name: str,
    tool_input: dict,
    *,
    cwd: str = "/home/ada/repo",
    stop_reason: str = "tool_use",
) -> str:
    """A single-record assistant transcript with one tool_use block."""
    return json.dumps(
        {
            "type": "assistant",
            "cwd": cwd,
            "message": {
                "id": "msg_test",
                "stop_reason": stop_reason,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_test",
                        "name": tool_name,
                        "input": tool_input,
                    }
                ],
            },
        }
    )


def _parse_one(line: str) -> list:
    return ClaudeCodeObserver().parse(line, 0)


@pytest.mark.parametrize(
    ("tool", "tool_input", "mode", "path"),
    [
        pytest.param(
            "Write", {"file_path": "/home/ada/repo/src/app.py"}, "write", "src/app.py", id="write"
        ),
        pytest.param(
            "Edit", {"file_path": "/home/ada/repo/lib/mod.ts"}, "write", "lib/mod.ts", id="edit"
        ),
        # MultiEdit batches several edits to one file; it names the file once.
        pytest.param(
            "MultiEdit",
            {
                "file_path": "/home/ada/repo/config.py",
                "edits": [
                    {"old_string": "a", "new_string": "b"},
                    {"old_string": "c", "new_string": "d"},
                ],
            },
            "write",
            "config.py",
            id="multiedit-names-the-file-once",
        ),
        pytest.param(
            "NotebookEdit",
            {"notebook_path": "/home/ada/repo/nb.ipynb"},
            "write",
            "nb.ipynb",
            id="notebookedit",
        ),
        pytest.param(
            "Read", {"file_path": "/home/ada/repo/README.md"}, "read", "README.md", id="read"
        ),
    ],
)
def test_file_tools_yield_one_path_in_their_mode(tool, tool_input, mode, path):
    (event,) = _parse_one(_tool_use_record(tool, tool_input))
    assert event.kind is EventKind.TOOL_CALL
    assert event.tool_name == tool
    assert len(event.paths) == 1
    assert event.paths[0].mode == mode
    assert event.paths[0].path == path


@pytest.mark.parametrize(
    "record",
    [
        # A shell command is not a named file; parsing it would be guessing.
        pytest.param(
            _tool_use_record("Bash", {"command": "cat src/app.py && rm -f /tmp/trash"}),
            id="shell-command",
        ),
        # A tool the adapter does not recognise yields nothing, not a guess.
        pytest.param(
            _tool_use_record("Frobnicate", {"file_path": "/home/ada/repo/x.py"}),
            id="unknown-tool",
        ),
        # A file outside the repo is not recall's business: relative would be a lie and
        # absolute a privacy breach.
        pytest.param(
            _tool_use_record(
                "Write", {"file_path": "/home/ada/.config/secret.json"}, cwd="/home/ada/repo"
            ),
            id="outside-cwd",
        ),
    ],
)
def test_calls_without_a_recallable_file_yield_no_paths(record):
    (event,) = _parse_one(record)
    assert event.paths == ()


def test_tool_use_with_scrubbed_input_yields_no_paths():
    """The real fixture scrubs inputs; that must not crash or guess."""
    record = json.dumps(
        {
            "type": "assistant",
            "cwd": "/tmp/theater-fixture",
            "message": {
                "id": "msg_vrtx_01243Q49QvUJc9PDV3afi8My",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_vrtx_01QnKbVKta8SLahVL3DFnGjP",
                        "name": "Read",
                        "input": {"scrubbed": True},
                    }
                ],
            },
        }
    )
    (event,) = _parse_one(record)
    assert event.tool_name == "Read"
    assert event.paths == ()


def test_relative_path_is_kept_as_is():
    record = _tool_use_record("Edit", {"file_path": "src/app.py"})
    (event,) = _parse_one(record)
    assert event.paths[0].path == "src/app.py"


def test_no_cwd_drops_absolute_path():
    """A record without cwd (rare, first permission-mode record) cannot be
    relativised, so the path is dropped rather than leaked as absolute."""
    record = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "msg_test",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_test",
                        "name": "Write",
                        "input": {"file_path": "/home/ada/repo/x.py"},
                    }
                ],
            },
        }
    )
    (event,) = _parse_one(record)
    assert event.paths == ()


def test_cwd_with_trailing_slash_relativises_correctly():
    record = _tool_use_record(
        "Read",
        {"file_path": "/home/ada/repo/x.py"},
        cwd="/home/ada/repo/",
    )
    (event,) = _parse_one(record)
    assert event.paths[0].path == "x.py"
