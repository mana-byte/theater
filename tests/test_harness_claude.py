"""Claude Code's file layout, as opposed to its record format.

The record format is covered in test_harness_parse.py against a scrubbed real
transcript. What is left, and what these tests are about, is everything the
adapter does with the *directory*: choosing which of ~/.claude/projects/*/*.jsonl
belongs to a participant, and reading Task sidechains out of it.

Picking the wrong file is the expensive mistake. Claude names each transcript
after its session id and files it under a slug of the working directory, so
two agents in one repo differ only by a uuid nobody has yet. The adapter
therefore reads `cwd` out of each candidate's own records rather than trusting
the slug — and gives up, rather than guessing, when it cannot.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from shipped import ClaudeCodeHarness, ClaudeCodeObserver

from theater.harness import theater_mcp_servers
from theater.models import BadRequest

SESSION = "b67b4276-f8b8-43ed-9987-0b5b3828c8cd"


def write(path: Path, records: list[dict | str]) -> Path:
    """A transcript file, with raw strings passed through unencoded."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path) -> Path:
    d = tmp_path / "projects"
    d.mkdir()
    return d


@pytest.fixture
def observer(root) -> ClaudeCodeObserver:
    return ClaudeCodeObserver(root=root)


# ---- finding the transcript ----------------------------------------------


def test_a_missing_projects_directory_is_not_an_error(tmp_path):
    """Claude may simply not be installed; that is a None, not a crash."""
    observer = ClaudeCodeObserver(root=tmp_path / "never-created")
    assert observer.find_transcript(cwd="/tmp/x") is None


def test_a_known_session_id_is_found_by_filename_without_reading_anything(root, observer):
    """The stem *is* the session id, so this needs no scan and no cwd guess."""
    want = write(root / "-tmp-x" / f"{SESSION}.jsonl", [{"cwd": "/somewhere/else"}])
    assert observer.find_transcript(cwd="/tmp/x", session_id=SESSION) == want


def test_an_unknown_session_id_falls_back_to_the_cwd_scan(root, observer):
    """A stale id must not shadow a transcript that is really there."""
    want = write(root / "-tmp-x" / "aaa.jsonl", [{"cwd": "/tmp/x"}])
    assert observer.find_transcript(cwd="/tmp/x", session_id="not-on-disk") == want


def test_without_a_cwd_there_is_nothing_to_match_on(observer):
    assert observer.find_transcript(cwd="") is None


def test_the_newest_transcript_wins_among_several_in_one_directory(root, observer):
    """Two agents in one repo differ only by mtime until one of them speaks."""
    old = write(root / "-tmp-x" / "old.jsonl", [{"cwd": "/tmp/x"}])
    new = write(root / "-tmp-x" / "new.jsonl", [{"cwd": "/tmp/x"}])
    import os

    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert observer.find_transcript(cwd="/tmp/x") == new


def test_a_transcript_older_than_the_participant_is_not_theirs(root, observer):
    """Without this, a respawn in the same repo re-attaches to the dead session."""
    write(root / "-tmp-x" / "aaa.jsonl", [{"cwd": "/tmp/x"}])
    assert observer.find_transcript(cwd="/tmp/x", after=0) is not None
    assert observer.find_transcript(cwd="/tmp/x", after=time.time() + 3600) is None


def test_a_transcript_for_another_directory_is_ignored(root, observer):
    write(root / "-tmp-y" / "aaa.jsonl", [{"cwd": "/tmp/y"}])
    assert observer.find_transcript(cwd="/tmp/x") is None


def test_the_cwd_is_resolved_before_it_is_compared(root, observer, tmp_path):
    """`/tmp` is a symlink to `/private/tmp` on macOS; the two must still match."""
    real = tmp_path / "repo"
    real.mkdir()
    write(root / "-repo" / "aaa.jsonl", [{"cwd": str(real)}])
    assert observer.find_transcript(cwd=f"{real}/.") is not None


def test_a_malformed_line_does_not_hide_the_cwd_on_the_next_one(root, observer):
    """Claude writes partial lines while flushing; one is not a reason to give up."""
    want = write(root / "-tmp-x" / "aaa.jsonl", ['{"cwd": "/tmp/x"', {"cwd": "/tmp/x"}])
    assert observer.find_transcript(cwd="/tmp/x") == want


def test_the_cwd_probe_gives_up_rather_than_read_a_whole_transcript(root, observer):
    """A transcript can be megabytes; the cwd is in the first records or nowhere."""
    filler: list[dict | str] = [{"type": "system"} for _ in range(25)]
    write(root / "-tmp-x" / "aaa.jsonl", [*filler, {"cwd": "/tmp/x"}])
    assert observer.find_transcript(cwd="/tmp/x") is None


def test_a_transcript_that_cannot_be_read_is_skipped_not_fatal(root, observer, monkeypatch):
    write(root / "-tmp-x" / "aaa.jsonl", [{"cwd": "/tmp/x"}])
    original = Path.open

    def deny(self, *a, **kw):
        if self.suffix == ".jsonl":
            raise OSError("permission denied")
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, "open", deny)
    assert observer.find_transcript(cwd="/tmp/x") is None


def test_the_session_id_is_read_off_the_filename(observer):
    assert observer.session_id(Path(f"/x/{SESSION}.jsonl")) == SESSION


# ---- Task sidechains ------------------------------------------------------


def test_each_sidechain_is_reported_once_however_many_records_it_has(root, observer):
    """A Task subagent writes a whole conversation; it is still one child."""
    path = write(
        root / "-tmp-x" / "aaa.jsonl",
        [
            {"isSidechain": False, "uuid": "main-1"},
            {"isSidechain": True, "uuid": "task-1", "parentUuid": None},
            {"isSidechain": True, "uuid": "task-2", "parentUuid": "task-1"},
            {"isSidechain": True, "uuid": "task-3", "parentUuid": "task-1"},
        ],
    )
    children = observer.native_children(path)
    assert [(c.session_id, c.agent) for c in children] == [("task-1", "task")]


def test_two_separate_tasks_are_two_children(root, observer):
    path = write(
        root / "-tmp-x" / "aaa.jsonl",
        [
            {"isSidechain": True, "uuid": "task-a", "parentUuid": None},
            {"isSidechain": True, "uuid": "task-b", "parentUuid": None},
        ],
    )
    assert [c.session_id for c in observer.native_children(path)] == ["task-a", "task-b"]


def test_a_transcript_with_no_sidechains_has_no_children(root, observer):
    path = write(root / "-tmp-x" / "aaa.jsonl", [{"isSidechain": False, "uuid": "main-1"}])
    assert observer.native_children(path) == []


def test_blank_and_malformed_lines_are_skipped_while_scanning_for_children(root, observer):
    path = write(
        root / "-tmp-x" / "aaa.jsonl",
        ["", "not json", "[1, 2]", {"isSidechain": True, "uuid": "task-1"}],
    )
    assert [c.session_id for c in observer.native_children(path)] == ["task-1"]


def test_a_sidechain_with_no_uuid_at_all_is_not_a_child(root, observer):
    """Nothing to name it by; inventing one would create a phantom in the tree."""
    path = write(root / "-tmp-x" / "aaa.jsonl", [{"isSidechain": True}])
    assert observer.native_children(path) == []


def test_children_of_a_transcript_that_is_gone_is_empty_not_an_error(tmp_path, observer):
    """The pane can die between the observer's decision to read and the read."""
    assert observer.native_children(tmp_path / "never-written.jsonl") == []


# ---- launch ---------------------------------------------------------------


def _argv(approval: str, tmp_path: Path) -> list[str]:
    plan = ClaudeCodeHarness().plan_launch(
        participant_id="p-abc",
        prompt="say hello",
        config_path=tmp_path / "mcp.json",
        approval=approval,
    )
    return plan.argv


def test_an_unknown_approval_is_refused_before_a_pane_is_opened(tmp_path):
    """The alternative is a window that dies on an argument claude rejects."""
    with pytest.raises(BadRequest) as exc:
        _argv("whatever", tmp_path)
    assert "approval must be one of" in str(exc.value)


def test_edits_asks_claude_to_accept_edits_and_nothing_more(tmp_path):
    argv = _argv("edits", tmp_path)
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in argv


def test_yolo_skips_permissions(tmp_path):
    assert "--dangerously-skip-permissions" in _argv("yolo", tmp_path)


def test_manual_adds_no_permission_flag_at_all(tmp_path):
    argv = _argv("manual", tmp_path)
    assert "--permission-mode" not in argv
    assert "--dangerously-skip-permissions" not in argv


def test_the_mcp_config_is_bound_with_an_equals_sign(tmp_path):
    """`--mcp-config <path>` is variadic and swallows the prompt positional."""
    argv = _argv("manual", tmp_path)
    assert argv[1] == f"--mcp-config={tmp_path / 'mcp.json'}"
    assert argv[-1] == "say hello"


def test_an_empty_prompt_leaves_claude_waiting_rather_than_running_nothing(tmp_path):
    plan = ClaudeCodeHarness().plan_launch(
        participant_id="p-abc",
        prompt="",
        config_path=tmp_path / "mcp.json",
        approval="manual",
    )
    assert plan.argv[:2] == ["claude", f"--mcp-config={tmp_path / 'mcp.json'}"]
    assert plan.argv[2].startswith("--settings=")
    assert plan.argv[3] == f"--session-id={plan.session_id}"
    assert len(plan.argv) == 4


def test_the_config_written_alongside_names_this_participant(tmp_path):
    plan = ClaudeCodeHarness().plan_launch(
        participant_id="p-abc",
        prompt="hi",
        config_path=tmp_path / "mcp.json",
        approval="manual",
        mcp_servers=theater_mcp_servers("p-abc", "claude"),
    )
    config = json.loads(plan.files[tmp_path / "mcp.json"])
    assert config["mcpServers"]["theater"]["args"] == [
        "mcp",
        "--id",
        "p-abc",
        "--harness",
        "claude",
        "--toolset",
        "control",
    ]
    assert config["mcpServers"]["theater_wait"]["args"][-1] == "wait"


# ---- timestamps -----------------------------------------------------------


def test_a_timestamp_claude_did_not_write_leaves_the_event_unstamped(observer):
    """The observer stamps its own time; a wrong one would reorder the feed."""
    record = {
        "type": "assistant",
        "timestamp": "the day before yesterday",
        "message": {
            "id": "m1",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hi"}],
        },
    }
    (event,) = observer.parse(json.dumps(record), 0)
    assert event.ts is None
    assert event.turn_end
