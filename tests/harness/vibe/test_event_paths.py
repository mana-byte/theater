"""EventPath extraction from Vibe transcript records."""

from __future__ import annotations

import json

from tests.support.shipped import VibeObserver
from theater.harness import EventPath
from theater.harness.base import EventKind

REPO = "/home/alice/project"


def _assistant_with_tools(*calls: dict) -> str:
    return json.dumps(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": f"call-{i}", "index": i, "function": c} for i, c in enumerate(calls)
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
        _assistant_with_tools(_fn("read_file", file_path="/etc/passwd")),
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
