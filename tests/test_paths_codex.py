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
from pathlib import Path

from shipped import CodexHarness, CodexObserver

from theater.harness import plan_launch
from theater.harness.base import EventKind, EventPath

FIXTURES = Path(__file__).parent / "fixtures"

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
    "*** Begin Patch\n"
    "*** Update File: README.md\n"
    "@@ -1 +1 @@\n"
    "-# old\n"
    "+# new\n"
    "*** End Patch\n"
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


def test_apply_patch_add_file_is_a_write():
    patch = "*** Begin Patch\n*** Add File: src/new.py\n+pass\n*** End Patch\n"
    events = _parse(_apply_patch_call(patch))
    assert events[0].paths == (EventPath(path="src/new.py", mode="write"),)


def test_apply_patch_delete_file_is_a_write():
    patch = "*** Begin Patch\n*** Delete File: src/gone.py\n*** End Patch\n"
    events = _parse(_apply_patch_call(patch))
    assert events[0].paths == (EventPath(path="src/gone.py", mode="write"),)


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
            "arguments": json.dumps(
                {"command": "cat src/main.rs", "workdir": "/tmp/repo"}
            ),
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


def test_empty_apply_patch_input_yields_no_paths():
    events = _parse(_apply_patch_call(""))
    assert events[0].paths == ()


# ---- repo-relative guarantee -------------------------------------------


def test_apply_patch_paths_are_repo_relative_never_absolute():
    """The patch grammar uses repo-relative paths. No absolute path should
    appear in EventPath, because the touch index gets pasted into agent
    prompts and an absolute path leaks the developer's home directory."""
    events = _parse(_apply_patch_call(_PATCH_THREE_FILES))
    for ep in events[0].paths:
        assert not Path(ep.path).is_absolute(), ep.path


# ---- the real fixture: exec call in codex.jsonl yields no paths --------


def test_codex_fixture_exec_call_yields_no_paths():
    """The shipped fixture's custom_tool_call is an ``exec`` call with a
    placeholder input. It must yield no paths — the placeholder is not a
    patch, and exec is not apply_patch."""
    lines = (FIXTURES / "codex.jsonl").read_text().splitlines()
    observer = CodexObserver()
    for i, line in enumerate(lines):
        record = json.loads(line)
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload", {})
        if payload.get("type") != "custom_tool_call":
            continue
        events = observer.parse(line, i)
        assert len(events) == 1
        assert events[0].paths == ()


# ---- resume argv -------------------------------------------------------


def test_resume_inserts_subcommand_and_session_id(tmp_path):
    """``codex resume <SESSION_ID>`` is a subcommand (cli/src/main.rs:181-182,
    315-339), not a flag. The session id is a positional after the subcommand,
    and the MCP config overrides and approval flags follow just as they do for
    a fresh launch."""
    plan = plan_launch(
        "codex",
        participant_id="abc123",
        prompt="continue working",
        config_path=tmp_path / "x.json",
        approval="manual",
        resume="019ff5c6-717c-7a70-9ec4-66dd1f4d173e",
    )
    assert plan.argv[0] == "codex"
    assert plan.argv[1] == "resume"
    assert plan.argv[2] == "019ff5c6-717c-7a70-9ec4-66dd1f4d173e"
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
    assert plan.argv[1] == "resume"
    assert plan.argv[2] == "some-session"
    # No trailing positional prompt.
    assert not plan.argv[-1].startswith("mcp_servers")
    assert plan.argv[-1] not in ("", "some-session")


def test_resume_takes_prompt_is_true():
    """Codex's resume subcommand still accepts and delivers the prompt
    positionally (cli/src/main.rs:3583-3595, 3616-3624 confirm this in the
    CLI's own tests). So the default ``True`` is correct."""
    assert CodexHarness.resume_takes_prompt is True


def test_supports_resume_is_true():
    from theater.harness import supports_resume

    assert supports_resume(CodexHarness()) is True
