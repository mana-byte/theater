"""EventPath extraction from Claude Code transcripts.

The recall index is built from ``Event.paths`` that the observer populates
during parse. These tests cover the claude adapter's extraction of file paths
from tool_use blocks: which tools yield paths, which mode (read/write) they
get, and the guarantee that every path is repo-relative.

The shipped fixture (``claude_code.jsonl``) scrubs tool inputs to
``{"scrubbed": true}``, so it exercises the parse pipeline but cannot exercise
path extraction. The tests here use synthetic records that carry the real
Claude Code tool input shapes (``file_path``, ``notebook_path``) alongside a
``cwd`` field, as every real record does.
"""

from __future__ import annotations

import json
from pathlib import Path

from shipped import ClaudeCodeHarness, ClaudeCodeObserver

from theater.harness.base import EventKind

FIXTURES = Path(__file__).parent / "fixtures"


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


# ---- write tools ----------------------------------------------------------


def test_write_yields_a_write_path():
    record = _tool_use_record("Write", {"file_path": "/home/ada/repo/src/app.py"})
    (event,) = _parse_one(record)
    assert event.kind is EventKind.TOOL_CALL
    assert event.tool_name == "Write"
    assert len(event.paths) == 1
    assert event.paths[0].mode == "write"
    assert event.paths[0].path == "src/app.py"


def test_edit_yields_a_write_path():
    record = _tool_use_record("Edit", {"file_path": "/home/ada/repo/lib/mod.ts"})
    (event,) = _parse_one(record)
    assert event.paths[0].mode == "write"
    assert event.paths[0].path == "lib/mod.ts"


def test_multiedit_yields_one_write_path_for_the_file():
    """MultiEdit batches several edits to one file; it names the file once."""
    record = _tool_use_record(
        "MultiEdit",
        {
            "file_path": "/home/ada/repo/config.py",
            "edits": [
                {"old_string": "a", "new_string": "b"},
                {"old_string": "c", "new_string": "d"},
            ],
        },
    )
    (event,) = _parse_one(record)
    assert len(event.paths) == 1
    assert event.paths[0].mode == "write"
    assert event.paths[0].path == "config.py"


def test_notebookedit_yields_a_write_path():
    record = _tool_use_record(
        "NotebookEdit", {"notebook_path": "/home/ada/repo/nb.ipynb"}
    )
    (event,) = _parse_one(record)
    assert event.paths[0].mode == "write"
    assert event.paths[0].path == "nb.ipynb"


# ---- read tools -----------------------------------------------------------


def test_read_yields_a_read_path():
    record = _tool_use_record("Read", {"file_path": "/home/ada/repo/README.md"})
    (event,) = _parse_one(record)
    assert event.paths[0].mode == "read"
    assert event.paths[0].path == "README.md"


# ---- no-path tools --------------------------------------------------------


def test_bash_yields_no_paths():
    """A shell command is not a named file; parsing it would be guessing."""
    record = _tool_use_record(
        "Bash", {"command": "cat src/app.py && rm -f /tmp/trash"}
    )
    (event,) = _parse_one(record)
    assert event.paths == ()


def test_glob_yields_no_paths():
    """Glob takes a pattern, not a named file."""
    record = _tool_use_record("Glob", {"pattern": "**/*.py", "path": "/home/ada/repo"})
    (event,) = _parse_one(record)
    assert event.paths == ()


def test_grep_yields_no_paths():
    """Grep searches content, not a named file."""
    record = _tool_use_record(
        "Grep", {"pattern": "TODO", "path": "/home/ada/repo/src", "output_mode": "content"}
    )
    (event,) = _parse_one(record)
    assert event.paths == ()


def test_unknown_tool_yields_no_paths():
    """A tool the adapter does not recognise yields nothing, not a guess."""
    record = _tool_use_record("Frobnicate", {"file_path": "/home/ada/repo/x.py"})
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


# ---- repo-relative guarantee ---------------------------------------------


def test_absolute_path_is_relativised_against_cwd():
    record = _tool_use_record(
        "Write", {"file_path": "/home/ada/repo/deep/nested/file.py"}
    )
    (event,) = _parse_one(record)
    assert event.paths[0].path == "deep/nested/file.py"
    assert not event.paths[0].path.startswith("/")


def test_path_outside_cwd_is_dropped():
    """A file outside the repo is not recall's business; emitting it relative
    would be a lie, and emitting it absolute is a privacy breach."""
    record = _tool_use_record(
        "Write",
        {"file_path": "/home/ada/.config/secret.json"},
        cwd="/home/ada/repo",
    )
    (event,) = _parse_one(record)
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


# ---- fixture integration -------------------------------------------------


def test_real_fixture_parses_without_paths_due_to_scrubbed_inputs():
    """The shipped fixture has scrubbed inputs, so no paths are extracted.

    This confirms the pipeline runs clean against real transcript structure
    and that scrubbed inputs yield empty paths rather than errors."""
    lines = (FIXTURES / "claude_code.jsonl").read_text().splitlines()
    observer = ClaudeCodeObserver()
    events = []
    for i, line in enumerate(lines):
        events.extend(observer.parse(line, i))
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "Read"
    # Scrubbed input has no file_path key, so no paths.
    assert tool_calls[0].paths == ()


# ---- resume support -------------------------------------------------------


def test_plan_launch_with_resume_adds_the_flag(tmp_path):
    plan = ClaudeCodeHarness().plan_launch(
        participant_id="p-abc",
        prompt="continue",
        config_path=tmp_path / "mcp.json",
        approval="manual",
        resume="session-uuid-123",
    )
    assert "--resume=session-uuid-123" in plan.argv
    assert plan.argv[-1] == "continue"


def test_plan_launch_without_resume_adds_no_resume_flag(tmp_path):
    plan = ClaudeCodeHarness().plan_launch(
        participant_id="p-abc",
        prompt="hello",
        config_path=tmp_path / "mcp.json",
        approval="manual",
    )
    assert not any(arg.startswith("--resume") for arg in plan.argv)
