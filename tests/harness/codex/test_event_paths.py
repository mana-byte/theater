"""Codex parser Event.path extraction from apply_patch, and the resume argv."""

from __future__ import annotations

import json

import pytest

from tests.support.shipped import CodexObserver
from theater.harness import plan_launch
from theater.harness.base import EventKind, EventPath

# Every other tool call embeds paths in code or command strings, which the recall contract
# refuses to parse: a wrong path in the touch index is worse than a missing one.
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


@pytest.mark.parametrize(
    "payload",
    [
        # The ``exec`` custom tool takes freeform JavaScript source: paths appear only embedded
        # in code, which is prose to the recall contract, not a structured field.
        pytest.param(
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c1",
                "input": "const fs = require('fs'); fs.readFileSync('src/main.rs')",
            },
            id="exec-source",
        ),
        # ``shell_command`` carries a ``workdir`` directory, not a file read or written, inside
        # a JSON blob in the CLI's own dialect.
        pytest.param(
            {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "c2",
                "arguments": json.dumps({"command": "cat src/main.rs", "workdir": "/tmp/repo"}),
            },
            id="shell-command",
        ),
        pytest.param(
            {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": [{"type": "input_text", "text": "done"}],
            },
            id="tool-result",
        ),
        # A garbled input is not a partial guess: no paths, not maybe-paths.
        pytest.param(_apply_patch_call("not a patch at all"), id="malformed-apply-patch"),
    ],
)
def test_events_without_a_structured_file_field_yield_no_paths(payload):
    events = _parse(payload)
    assert len(events) == 1
    assert events[0].paths == ()


# ---- resume argv -------------------------------------------------------


def test_resume_forks_the_session(tmp_path, available_harness_binaries):
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
    assert "-a" in plan.argv and "on-request" in plan.argv
    # The prompt is still delivered positionally.
    assert plan.argv[-1] == "continue working"


def test_resume_without_prompt_omits_positional(tmp_path, available_harness_binaries):
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
