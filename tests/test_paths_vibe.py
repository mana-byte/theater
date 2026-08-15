"""EventPath extraction from vibe's transcript records.

Vibe's ``messages.jsonl`` stores tool calls with ``function.arguments`` as a
JSON string. The file-path-taking tools (``read_file``, ``write_file``,
``edit``) carry absolute paths there, and the recall index needs them
repo-relative. These tests pin the extraction, the relativisation, the
no-path cases, and the resume argv.
"""

from __future__ import annotations

import json

from shipped import VibeHarness, VibeObserver

from theater.harness import EventPath, plan_launch
from theater.harness.base import EventKind

REPO = "/home/alice/project"


def _assistant_with_tools(*calls: dict) -> str:
    return json.dumps(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": f"call-{i}", "index": i, "function": c}
                for i, c in enumerate(calls)
            ],
        }
    )


def _fn(name: str, **args: object) -> dict:
    return {"name": name, "arguments": json.dumps(args)}


def _parse(observer: VibeObserver, line: str) -> list:
    return observer.parse(line, 0)


def _set_cwd(observer: VibeObserver, cwd: str) -> None:
    """Set the cwd the way find_transcript would."""
    observer._cwd = cwd


# ---- write -------------------------------------------------------------


def test_write_file_emits_write_path():
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(
        observer,
        _assistant_with_tools(_fn("write_file", file_path=f"{REPO}/src/main.py")),
    )
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert len(tool_calls) == 1
    assert tool_calls[0].paths == (EventPath("src/main.py", "write"),)


def test_edit_emits_write_path():
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(
        observer,
        _assistant_with_tools(_fn("edit", file_path=f"{REPO}/README.md")),
    )
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert tool_calls[0].paths == (EventPath("README.md", "write"),)


# ---- read --------------------------------------------------------------


def test_read_file_emits_read_path():
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(
        observer,
        _assistant_with_tools(_fn("read_file", file_path=f"{REPO}/lib/util.py")),
    )
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert tool_calls[0].paths == (EventPath("lib/util.py", "read"),)


# ---- multi-file --------------------------------------------------------


def test_multiple_tool_calls_each_emit_their_own_paths():
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(
        observer,
        _assistant_with_tools(
            _fn("read_file", file_path=f"{REPO}/a.py"),
            _fn("edit", file_path=f"{REPO}/b.py"),
            _fn("write_file", file_path=f"{REPO}/c.py"),
        ),
    )
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert len(tool_calls) == 3
    assert tool_calls[0].paths == (EventPath("a.py", "read"),)
    assert tool_calls[1].paths == (EventPath("b.py", "write"),)
    assert tool_calls[2].paths == (EventPath("c.py", "write"),)


# ---- no paths ----------------------------------------------------------


def test_bash_emits_no_paths():
    """Shell commands are not parsed for file paths."""
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(
        observer,
        _assistant_with_tools(_fn("bash", command=f"cat {REPO}/secret.txt")),
    )
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert tool_calls[0].paths == ()


def test_grep_emits_no_paths():
    """grep's path is a search root (file or dir), not a file being read."""
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(
        observer,
        _assistant_with_tools(_fn("grep", path=f"{REPO}/src", pattern="TODO")),
    )
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert tool_calls[0].paths == ()


def test_unknown_tool_emits_no_paths():
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(
        observer,
        _assistant_with_tools(_fn("web_fetch", url="https://example.com")),
    )
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert tool_calls[0].paths == ()


def test_assistant_text_only_emits_no_paths():
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(observer, json.dumps({"role": "assistant", "content": "hi"}))
    assert all(e.paths == () for e in events)


def test_user_record_emits_no_paths():
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(observer, json.dumps({"role": "user", "content": "hello"}))
    assert all(e.paths == () for e in events)


def test_tool_result_emits_no_paths():
    """Paths come from the tool call, not the tool result."""
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(
        observer,
        json.dumps(
            {"role": "tool", "content": "file contents", "name": "read_file"}
        ),
    )
    assert all(e.paths == () for e in events)


# ---- repo-relative guarantee -------------------------------------------


def test_absolute_path_is_relativised():
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(
        observer,
        _assistant_with_tools(_fn("read_file", file_path=f"{REPO}/deep/nested/file.ts")),
    )
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    path = tool_calls[0].paths[0].path
    assert not path.startswith("/")
    assert path == "deep/nested/file.ts"


def test_relative_path_passes_through():
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(
        observer,
        _assistant_with_tools(_fn("read_file", file_path="src/main.py")),
    )
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert tool_calls[0].paths[0].path == "src/main.py"


def test_path_outside_cwd_is_dropped():
    """A path not under the repo root is not emitted as an absolute."""
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    events = _parse(
        observer,
        _assistant_with_tools(
            _fn("read_file", file_path="/etc/passwd")
        ),
    )
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert tool_calls[0].paths == ()


def test_no_cwd_drops_absolute_path():
    """Without a cwd, an absolute path cannot be relativised and is dropped."""
    observer = VibeObserver()
    # _cwd is None by default — find_transcript was never called
    events = _parse(
        observer,
        _assistant_with_tools(_fn("read_file", file_path=f"{REPO}/src/main.py")),
    )
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert tool_calls[0].paths == ()


# ---- turn_end preserves paths ------------------------------------------


def test_turn_end_event_preserves_paths():
    """The boundary reconstruction must not drop paths from the last event."""
    observer = VibeObserver()
    _set_cwd(observer, REPO)
    record = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "index": 0,
                "function": _fn("write_file", file_path=f"{REPO}/done.py"),
            }
        ],
    }
    # A record WITH tool_calls does NOT end the turn, so this tests the
    # non-boundary case. The boundary case is when there are no tool_calls
    # but there IS content — which carries no paths anyway. The important
    # thing is that the boundary reconstruction at the end of parse carries
    # paths through when the last event is a tool call.
    events = _parse(observer, json.dumps(record))
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert tool_calls[0].paths == (EventPath("done.py", "write"),)


# ---- resume argv (Task B) ----------------------------------------------


def test_vibe_resume_uses_resume_flag(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="abc",
        prompt="do something",
        config_path=tmp_path / "x.json",
        approval="manual",
        resume="session-xyz",
    )
    assert "--resume" in plan.argv
    assert plan.argv[plan.argv.index("--resume") + 1] == "session-xyz"
    assert plan.argv[-1] == "do something"


def test_vibe_resume_without_prompt(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="abc",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
        resume="session-xyz",
    )
    assert "--resume" in plan.argv
    assert "session-xyz" in plan.argv
    assert plan.argv[-1] == "session-xyz"


def test_vibe_resume_preserves_approval_flags(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="abc",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="yolo",
        resume="s1",
    )
    assert "--yolo" in plan.argv
    assert "--resume" in plan.argv


def test_vibe_resume_takes_prompt_is_true():
    """Vibe's positional prompt is honoured on resume.

    Evidence: vibe/cli/cli.py:258-264 passes ``initial_prompt`` to
    ``StartupOptions`` regardless of whether ``--resume`` was given, so the
    prompt reaches the TUI and is sent as a user message after session load.
    """
    assert VibeHarness.resume_takes_prompt is True


def test_vibe_without_resume_has_no_resume_flag(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="abc",
        prompt="hello",
        config_path=tmp_path / "x.json",
        approval="manual",
    )
    assert "--resume" not in plan.argv
