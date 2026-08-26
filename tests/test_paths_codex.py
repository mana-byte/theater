"""Event.path extraction in the codex parser.

Codex's transcript carries file paths in exactly one structured place: the
``apply_patch`` custom tool call, whose ``input`` is a raw patch text with
explicit per-file markers (``*** Update File:``, ``*** Add File:``, ``*** Delete
File:``). Every other tool call — ``exec`` (JavaScript source), ``shell_command``
(a JSON blob with a ``workdir`` directory), ``local_shell_call`` (a command vector
with a ``working_directory`` directory) — embeds paths inside code or command
strings, which the recall contract refuses to parse: a wrong path in the touch
index is worse than a missing one.

The patch markers are a structured grammar defined in
codex-rs/apply-patch/src/parser.rs:39-41, not prose, so extracting paths from
them is reading a structured field. The paths are repo-relative by construction:
codex's apply_patch applies them relative to the session cwd, so no
relativisation is needed inside parse.
"""

from __future__ import annotations

import json

from shipped import CodexObserver

from theater.daemon.trajectory.project import project_events_and_facts
from theater.harness import plan_launch
from theater.harness.base import EventKind, EventPath
from theater.trajectory import ContentFormat, TimingProvenance, TrajectoryKind
from theater.trajectory.tools import TrajectoryToolIdentity, tool_operations_for_records

#: A minimal apply_patch input that touches three files: one update, one add,
#: one delete. The patch body is irrelevant to path extraction — only the
#: ``*** <Verb> File: <path>`` lines matter.
_PATCH_THREE_FILES = (
    "*** Begin Patch\n"
    "*** Update File: src/main.rs\n"
    "@@ def example():\n"
    "-    pass\n"
    "+    return 123\n"
    "*** Add File: src/new_module.py\n"
    "+def helper():\n"
    "+    return True\n"
    "*** Delete File: src/old_module.py\n"
    "*** End Patch\n"
)

#: A single-file update patch.
_PATCH_ONE_FILE = (
    "*** Begin Patch\n*** Update File: README.md\n@@ -1 +1 @@\n-# old\n+# new\n*** End Patch\n"
)


def _parse(payload: dict, index: int = 0):
    """Parse one response_item record and return its events."""
    record = {"timestamp": "2026-08-12T11:40:50.100Z", "type": "response_item"}
    record["payload"] = payload
    return CodexObserver().parse(json.dumps(record), index)


def _apply_patch_call(input_text: str) -> dict:
    return {
        "type": "custom_tool_call",
        "id": "ctc_test",
        "call_id": "call_test",
        "name": "apply_patch",
        "input": input_text,
    }


# ---- write: apply_patch yields write-mode paths ------------------------


def test_apply_patch_single_file_update_is_a_write():
    events = _parse(_apply_patch_call(_PATCH_ONE_FILE))
    assert len(events) == 1
    event = events[0]
    assert event.kind is EventKind.TOOL_CALL
    assert event.tool_name == "apply_patch"
    assert event.paths == (EventPath(path="README.md", mode="write"),)


# ---- multi-file: one tool call, several EventPath entries ---------------


def test_apply_patch_with_multiple_files_yields_multiple_paths():
    events = _parse(_apply_patch_call(_PATCH_THREE_FILES))
    assert len(events) == 1
    paths = events[0].paths
    assert len(paths) == 3
    assert all(ep.mode == "write" for ep in paths)
    assert {ep.path for ep in paths} == {
        "src/main.rs",
        "src/new_module.py",
        "src/old_module.py",
    }


def test_patch_apply_end_uses_structured_changes_and_relativizes_paths():
    observer = CodexObserver()
    observer.parse(
        json.dumps({"type": "session_meta", "payload": {"cwd": "/repo"}}),
        0,
    )
    parsed = observer.parse_record(
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": "2026-08-25T21:14:41Z",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "exec-1",
                    "success": True,
                    "status": "completed",
                    "stdout": "Success",
                    "changes": {
                        "/repo/src/main.py": {
                            "type": "update",
                            "unified_diff": "@@ -1 +1 @@",
                            "move_path": "/repo/src/moved.py",
                        },
                        "/repo/src/new.py": {"type": "add", "content": "new"},
                        "/outside/secret.py": {"type": "delete", "content": "secret"},
                    },
                },
            }
        ),
        1,
    )
    events = parsed.events

    assert len(events) == 1
    assert events[0].kind is EventKind.TOOL_CALL
    assert events[0].tool_name == "apply_patch"
    assert events[0].paths == (
        EventPath("src/main.py", "write"),
        EventPath("src/moved.py", "write"),
        EventPath("src/new.py", "write"),
    )
    assert [fact.kind for fact in parsed.trajectory] == [
        TrajectoryKind.TOOL_CALL,
        TrajectoryKind.TOOL_RESULT,
    ]
    records = project_events_and_facts(
        parsed.baseline_events,
        parsed.trajectory,
        participant_id="participant",
        source_epoch="epoch",
        source="codex",
    )
    operation = tool_operations_for_records(records)[0]
    assert operation.identity is TrajectoryToolIdentity.MATCHED
    assert operation.tool_name == "apply_patch"
    assert any(field.name == "input" for field in operation.call_details)
    assert {
        field.preview.text for field in operation.call_details if field.format is ContentFormat.PATH
    } == {"src/main.py", "src/moved.py", "src/new.py"}
    result = next(field for field in operation.result_details if field.name == "result")
    assert '"success":true' in result.preview.text


def test_patch_apply_end_derives_timing_from_enclosing_exec():
    observer = CodexObserver()
    observer.parse_record(
        json.dumps(
            {
                "type": "response_item",
                "timestamp": "2026-08-25T21:14:40Z",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "outer",
                    "input": "const patch = await tools.apply_patch('patch')",
                },
            }
        ),
        0,
    )
    parsed = observer.parse_record(
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": "2026-08-25T21:14:41Z",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "inner",
                    "success": True,
                    "status": "completed",
                    "changes": {"src/main.py": {"type": "update"}},
                },
            }
        ),
        1,
    )
    records = project_events_and_facts(
        parsed.baseline_events,
        parsed.trajectory,
        participant_id="participant",
        source_epoch="epoch",
        source="codex",
    )

    operation = tool_operations_for_records(records)[0]
    assert operation.timing is not None
    assert operation.timing.duration_ms == 1_000
    assert operation.timing.provenance is TimingProvenance.DERIVED

    observer.parse_record(
        json.dumps(
            {
                "type": "response_item",
                "timestamp": "2026-08-25T21:14:41.1Z",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "outer",
                    "output": "done",
                },
            }
        ),
        2,
    )
    unmatched = observer.parse_record(
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": "2026-08-25T21:14:42Z",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "unmatched",
                    "success": True,
                    "status": "completed",
                    "changes": {"src/other.py": {"type": "update"}},
                },
            }
        ),
        3,
    )
    unmatched_records = project_events_and_facts(
        unmatched.baseline_events,
        unmatched.trajectory,
        participant_id="participant",
        source_epoch="epoch",
        source="codex",
    )
    unmatched_operation = tool_operations_for_records(unmatched_records)[0]
    assert unmatched_operation.timing is not None
    assert unmatched_operation.timing.duration_ms is None


def test_patch_apply_end_without_valid_changes_is_not_an_activity():
    observer = CodexObserver()
    observer.parse(
        json.dumps({"type": "session_meta", "payload": {"cwd": "/repo"}}),
        0,
    )
    events = observer.parse(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "success": False,
                    "changes": {"/repo/not-a-change": {"type": "unknown"}},
                },
            }
        ),
        1,
    )

    assert events == []


# ---- read: codex has no structured read tool ---------------------------


def test_exec_tool_call_yields_no_paths():
    """The ``exec`` custom tool takes freeform JavaScript source as input.

    File paths appear only embedded in code, which is prose from the recall
    contract's perspective — not a structured field. So exec yields nothing.
    """
    events = _parse(
        {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "c1",
            "input": "const fs = require('fs'); fs.readFileSync('src/main.rs')",
        }
    )
    assert len(events) == 1
    assert events[0].paths == ()


def test_function_call_shell_command_yields_no_paths():
    """The ``shell_command`` function call has a ``workdir`` in its arguments,
    but that is a directory, not a file being read or written — and the
    ``arguments`` string is a JSON blob whose structure is the CLI's dialect,
    not a structured field the harness gives us for path extraction."""
    events = _parse(
        {
            "type": "function_call",
            "name": "shell_command",
            "call_id": "c2",
            "arguments": json.dumps({"command": "cat src/main.rs", "workdir": "/tmp/repo"}),
        }
    )
    assert len(events) == 1
    assert events[0].paths == ()


# ---- no paths: events that should never carry paths --------------------


def test_tool_result_yields_no_paths():
    events = _parse(
        {
            "type": "custom_tool_call_output",
            "call_id": "c1",
            "output": [{"type": "input_text", "text": "done"}],
        }
    )
    assert len(events) == 1
    assert events[0].paths == ()


def test_malformed_apply_patch_input_yields_no_paths():
    """A garbled input is not a partial guess. No paths, not maybe-paths."""
    events = _parse(_apply_patch_call("not a patch at all"))
    assert events[0].paths == ()


# ---- resume argv -------------------------------------------------------


def test_resume_forks_the_session(tmp_path):
    plan = plan_launch(
        "codex",
        participant_id="abc123",
        prompt="continue working",
        config_path=tmp_path / "x.json",
        approval="manual",
        resume="019ff5c6-717c-7a70-9ec4-66dd1f4d173e",
    )
    assert plan.argv[0] == "codex"
    assert plan.argv[1] == "fork"
    assert plan.argv[2] == "019ff5c6-717c-7a70-9ec4-66dd1f4d173e"
    assert plan.session_id is None
    # The MCP config overrides and approval flags are still present.
    assert any(a.startswith("mcp_servers.theater.command=") for a in plan.argv)
    assert "-a" in plan.argv and "untrusted" in plan.argv
    # The prompt is still delivered positionally.
    assert plan.argv[-1] == "continue working"


def test_resume_without_prompt_omits_positional(tmp_path):
    plan = plan_launch(
        "codex",
        participant_id="abc",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
        resume="some-session",
    )
    assert plan.argv[1] == "fork"
    assert plan.argv[2] == "some-session"
    # No trailing positional prompt.
    assert not plan.argv[-1].startswith("mcp_servers")
    assert plan.argv[-1] not in ("", "some-session")
