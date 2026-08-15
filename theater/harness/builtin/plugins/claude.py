"""Claude Code.

Launch lever
------------
`--mcp-config FILE` reads a JSON file we write per participant, which is how
the participant id reaches the MCP server's argv. Deliberately *not*
`--strict-mcp-config`: the user's own servers should keep working inside a
Theater-spawned session.

Transcript layout
-----------------
    ~/.claude/projects/<slugged-cwd>/<sessionId>.jsonl

The directory name is the working directory with separators flattened to `-`,
but it is lossy — `/Users/ada.lovelace/x` and `/Users/ada/lovelace/x` slug
identically — so we never try to invert or reproduce it. Instead every
interesting record carries a verbatim `cwd` field, and that is what we match
on. The filename stem, on the other hand, *is* the sessionId (verified across
transcripts), so a known session id is an exact lookup with no scan at all.

Record shape, as observed over 3.4k records
-------------------------------------------
One content block per record. An assistant message with thinking, a preamble
and two tool calls is written as four records sharing `message.id` and
`message.stop_reason`. That means a turn-ending message can produce two
turn_end events. Harmless for status, which is idempotent, but not for jobs:
each boundary used to finish one waiting job, so a duplicate answered a second
caller with the first caller's reply. `message.id` is carried on the events as
`turn_id` and the observer answers each id once.

`stop_reason` is `tool_use` mid-turn and `end_turn` at the boundary, with a
handful of Nones. Treating "anything that is neither None nor tool_use" as the
end of a turn also covers `max_tokens` and `stop_sequence`, where the agent has
likewise stopped and is waiting.

Sidechains (`isSidechain: true`) are how Claude Code marks its own Task
sub-agents. The field is present on every record and was `false` on all 1103
records of the largest transcript, so native_children usually returns nothing.
That is a fact about how the harness gets used, not a bug here.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from theater.harness.base import (
    APPROVALS,
    SERVER_NAME,
    Event,
    EventKind,
    EventPath,
    Harness,
    LaunchPlan,
    NativeChild,
    clipper,
    last_screen_line,
    theater_binary,
)
from theater.harness.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
    TranscriptObserver,
)
from theater.models import BadRequest

#: Screen lines that mean "waiting for you". Anything after the prompt is
#: someone typing, which is presence, not idleness — so these stay exact.
IDLE_PROMPTS = (">", "> ")

#: Drawn by the CLI as the footer of every approval dialog, regardless of what
#: the question asks. Chosen over the question text (`Do you want to`) because
#: that phrasing can appear in an agent's own echoed output and would
#: false-positive. Present in `tests/fixtures/screens/claude_approval.txt`.
APPROVAL_MARKER = "Esc to cancel"

#: The workspace-trust onboarding dialog's primary option label. Unique to
#: that dialog and rendered on its own line (24 chars, so it does not wrap on
#: any realistic pane width). Unlike the header-ish strings `Accessing
#: workspace:` or `Quick safety check`, it cannot collide with the two
#: neighbour trust dialogs (`Trust this directory?` — the remote-control
#: add-server confirmation, and `Trust gateway <host>` — cloud-gateway TLS
#: pinning), which use different option labels entirely. The marker is an
#: option label, not body text, so it will not appear in echoed agent output.
TRUST_MARKER = "Yes, I trust this folder"

#: The middle segment of the status footer while a turn is in flight. The
#: footer keeps its shape and swaps this segment for `IDLE_FOOTER` when the
#: turn ends, so the two are mutually exclusive. Present in
#: `tests/fixtures/screens/claude_working.txt`.
WORKING_MARKER = "esc to interrupt"

#: The middle segment of the status footer while waiting for input. Used by
#: `screen_reading` because a real capture has this footer below the prompt,
#: so the prompt is not the last non-blank line and `is_idle_screen` — which
#: checks only the last line — does not fire.
#:
#: Deliberately not the permission-mode indicator (`manual mode on`) that sits
#: to its left: that is drawn while idle *and* while working, so it cannot
#: tell them apart, and its text changes with the approval mode the agent was
#: spawned under. Present in `tests/fixtures/screens/claude_idle.txt`.
IDLE_FOOTER = "? for shortcuts"

#: How far up from the bottom to look for the prompt and footer. A real
#: capture has several lines of padding below the footer, so a window of one
#: would miss it.
_SCREEN_TAIL_LINES = 6

#: Records to read before giving up on finding a `cwd` in a candidate
#: transcript. The first record is a `permission-mode` entry that has none;
#: `cwd` first appears around index 2.
_CWD_PROBE_RECORDS = 20

#: Individual records can be hundreds of KB. Bound the probe read so scanning
#: candidates never turns into reading whole transcripts.
_CWD_PROBE_BYTES = 256 * 1024


#: Tools that write a file, keyed by the input parameter that carries the path.
#: Each entry maps the tool name to the key whose value is the file path the
#: tool operates on. MultiEdit batches several edits to one file but names it
#: once, so it yields a single EventPath — the alternative, enumerating every
#: edit's path, would produce duplicates for the same file in the same event.
#:
#: The input shapes are not visible in the scrubbed fixture (every tool_use has
#: ``"input": {"scrubbed": true}``); the parameter names are grounded in the
#: Claude Code tool schema as referenced in the CHANGELOG (line 67 names
#: ``file_path``; line 550 names ``Write(path)``, ``NotebookEdit(path)``).
_WRITE_TOOLS: dict[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

#: Tools that read a file. Read and NotebookEdit-read both name the file
#: explicitly in a structured parameter. Grep and Glob take a path but it is
#: a directory or pattern, not a named file — see the note on _PATH_TOOLS.
_READ_TOOLS: dict[str, str] = {
    "Read": "file_path",
}


def _epoch(value) -> float | None:
    """Claude Code writes ISO-8601 with a Z suffix."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _relativise(path: str, cwd: str | None) -> str | None:
    """Make a path repo-relative, or return None if it cannot be done safely.

    Claude Code records absolute paths (e.g. ``/Users/ada/repo/src/app.py``)
    alongside a ``cwd`` field on every record. The recall index is pasted into
    agent prompts, so an absolute path that leaks ``/Users/<name>`` is a privacy
    breach, not a formatting issue.

    A relative path is returned as-is. An absolute path is stripped of the cwd
    prefix; if the cwd is not a prefix of the path (the file lives outside the
    repo), the path is dropped — emitting it relative would be a lie, and
    emitting it absolute is the breach. When cwd itself is None the path is
    dropped too, for the same reason.
    """
    if not path:
        return None
    if not path.startswith("/"):
        return path
    if cwd is None:
        return None
    # os.path.relpath would walk up with ``..`` for paths outside the cwd, which
    # is a valid relative path but not one that names a file inside the repo.
    # A path outside the working directory is not recall's business.
    c = cwd.rstrip("/") + "/"
    if not (path == cwd or path.startswith(c)):
        return None
    if path == cwd:
        return "."
    return path[len(c):]


class ClaudeCodeHarness(Harness):
    name = "claude"
    binary = "claude"
    #: Claude Code prints this same spoked asterisk as its own spinner glyph,
    #: so it reads as the product's mark rather than an arbitrary bullet.
    icon = "✻"
    #: What an agent might call itself at registration. A spelling that does not
    #: normalize is observed as nothing at all, so these are not cosmetic.
    aliases = ("claude_code", "claude-code", "Claude", "ClaudeCode")

    def __init__(self, root: Path | None = None):
        #: `root` locates the transcript, which is the observer's
        #: business alone; nothing about launching depends on it.
        self.observer = ClaudeCodeObserver(root=root)

    # ---- launching ------------------------------------------------------

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        resume: str | None = None,
    ) -> LaunchPlan:
        if approval not in APPROVALS:
            raise BadRequest(
                f"approval must be one of {', '.join(APPROVALS)}, got {approval!r}"
            )
        config = {
            "mcpServers": {
                SERVER_NAME: {
                    "command": theater_binary(),
                    "args": ["mcp", "--id", participant_id],
                }
            }
        }
        # `--mcp-config` is variadic in Claude Code 2.x (<configs...>,
        # space-separated). The space-separated form greedily consumes the
        # prompt positional as a second config path, so claude exits with
        # "MCP config file not found: <cwd>/<prompt>" and the pane dies before
        # the observer ever attaches. The `=` form binds the value tightly.
        argv = ["claude", f"--mcp-config={config_path}"]
        if model:
            # `=` form for the same reason as --mcp-config above: a
            # space-separated value sits next to the prompt positional, and
            # binding tightly is the habit that keeps this argv unambiguous.
            argv.append(f"--model={model}")
        if resume:
            # `--resume <session-id>` resumes a specific session by id or
            # name. Documented in the Claude Code CHANGELOG (line 2522:
            # "claude --resume <session-id>"; line 2526: "--resume <id>";
            # line 2587: "claude -p --resume <name>"). Interactive mode
            # reattaches to the session and still accepts a prompt positional,
            # which `resume_takes_prompt = True` (the Harness default) asserts.
            argv.append(f"--resume={resume}")
        if approval == "yolo":
            argv.append("--dangerously-skip-permissions")
        elif approval == "edits":
            argv += ["--permission-mode", "acceptEdits"]
        if prompt:
            argv.append(prompt)
        return LaunchPlan(
            argv=argv,
            files={config_path: json.dumps(config, indent=2) + "\n"},
        )


class ClaudeCodeObserver(TranscriptObserver):
    """Read `~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`.

    The only shipped observer whose parser can emit two turn boundaries for one
    native message — see the `turn_end` note at the top of this file. That is
    survivable today because the reducer answers a job per boundary; giving
    events a native turn id is what makes it harmless.
    """

    def __init__(self, root: Path | None = None):
        #: Injectable so tests never touch the real ~/.claude.
        self.root = root or Path.home() / ".claude" / "projects"

    def find_transcript(
        self,
        *,
        cwd: str,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        if not self.root.is_dir():
            return None
        if session_id:
            # The filename stem is the session id, so this needs no scan and no
            # guess about how the directory name was slugged.
            hit = next(self.root.glob(f"*/{session_id}.jsonl"), None)
            if hit is not None:
                return hit
        want = str(Path(cwd).resolve()) if cwd else None
        if want is None:
            return None
        candidates = []
        for path in self.root.glob("*/*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            if after is not None:
                born = getattr(st, "st_birthtime", st.st_ctime)
                if born < after:
                    continue
            candidates.append((st.st_mtime, path))
        for _, path in sorted(candidates, reverse=True):
            if self._transcript_cwd(path) == want:
                return path
        return None

    def _transcript_cwd(self, path: Path) -> str | None:
        read = 0
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    read += len(line)
                    if i >= _CWD_PROBE_RECORDS or read > _CWD_PROBE_BYTES:
                        return None
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    found = isinstance(record, dict) and record.get("cwd")
                    if found:
                        return str(Path(found).resolve())
        except OSError:
            return None
        return None

    def session_id(self, transcript: Path) -> str | None:
        """The filename is the session id. Verified, not assumed."""
        return transcript.stem or None

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        line = line.strip()
        if not line:
            return []
        try:
            record = json.loads(line)
        except ValueError:
            return []
        if not isinstance(record, dict):
            return []

        ts = _epoch(record.get("timestamp"))
        kind = record.get("type")
        message = record.get("message")
        message = message if isinstance(message, dict) else {}

        if kind == "assistant":
            return self._assistant(record, message, ts, index, clip_text=clip_text)
        if kind == "user":
            return self._user(message, ts, index, clip_text=clip_text)
        if kind == "system" and record.get("level") == "error":
            err = record.get("error")
            text = err if isinstance(err, str) else json.dumps(err, default=str)
            return [
                Event(
                    kind=EventKind.ERROR,
                    text=clipper(clip_text)(text),
                    ts=ts,
                    raw_index=index,
                    # An API error ends the attempt; the agent is waiting again.
                    turn_end=True,
                )
            ]
        return []

    def _assistant(
        self, record: dict, message: dict, ts: float | None, index: int,
        *, clip_text: bool = True,
    ) -> list[Event]:
        _clip = clipper(clip_text)

        stop = message.get("stop_reason")
        turn_end = stop is not None and stop != "tool_use"
        # `message.id` is shared by every record one message was split into,
        # which is exactly the set of records that can repeat a boundary. The
        # record's own `uuid` is not: it differs per record, so using it would
        # name each duplicate a separate turn and defeat the dedup. requestId
        # is the fallback for the handful of records written without a message
        # id; it is per API call, so it groups the same way for this purpose.
        tid = message.get("id") or record.get("requestId")
        tid = tid if isinstance(tid, str) and tid else None
        cwd = record.get("cwd")
        cwd = cwd if isinstance(cwd, str) and cwd else None
        out: list[Event] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                out.append(
                    Event(
                        kind=EventKind.ASSISTANT,
                        text=_clip(block.get("text")),
                        ts=ts,
                        turn_id=tid,
                        raw_index=index,
                    )
                )
            elif btype == "tool_use":
                out.append(
                    Event(
                        kind=EventKind.TOOL_CALL,
                        tool_name=block.get("name"),
                        ts=ts,
                        turn_id=tid,
                        raw_index=index,
                        paths=self._tool_paths(
                            block.get("name"),
                            block.get("input"),
                            cwd,
                        ),
                    )
                )
            # `thinking` is deliberately dropped: it is the agent's private
            # reasoning, it is the bulk of the bytes, and no consumer of the bus
            # needs it to answer "what is this agent doing".
        if turn_end:
            # A thinking-only record can end a turn. Never lose the boundary
            # just because the payload was filtered out.
            if out:
                last = out[-1]
                out[-1] = Event(
                    kind=last.kind,
                    text=last.text,
                    tool_name=last.tool_name,
                    ts=last.ts,
                    turn_end=True,
                    turn_id=tid,
                    raw_index=last.raw_index,
                    paths=last.paths,
                )
            else:
                out.append(
                    Event(
                        kind=EventKind.ASSISTANT,
                        ts=ts,
                        turn_end=True,
                        turn_id=tid,
                        raw_index=index,
                    )
                )
        return out

    def _tool_paths(
        self, name: str | None, tool_input: object, cwd: str | None
    ) -> tuple[EventPath, ...]:
        """Extract file paths from a tool_use block's structured input.

        Only tools whose input carries a named file path in a structured field
        yield EventPath entries — never paths parsed out of shell commands or
        prose. Bash, Glob, Grep, Task and similar tools take no single named
        file, so they yield nothing: a wrong path is worse than a missing one.

        Paths are relativised against the record's own ``cwd`` field, which
        every Claude Code record carries. An absolute path is made relative to
        that cwd; a path that is already relative is kept as-is. If the cwd is
        absent (rare, seen on the first permission-mode record), the path is
        dropped rather than emitting an absolute path that would leak a home
        directory into the recall index.
        """
        if not isinstance(tool_input, dict):
            return ()
        key = _WRITE_TOOLS.get(name or "") or _READ_TOOLS.get(name or "")
        if key is None:
            return ()
        raw = tool_input.get(key)
        if not isinstance(raw, str) or not raw:
            return ()
        mode = "write" if name in _WRITE_TOOLS else "read"
        rel = _relativise(raw, cwd)
        if rel is None:
            return ()
        return (EventPath(path=rel, mode=mode),)

    def _user(
        self, message: dict, ts: float | None, index: int, *, clip_text: bool = True
    ) -> list[Event]:
        _clip = clipper(clip_text)

        content = message.get("content")
        if isinstance(content, str):
            return [
                Event(
                    kind=EventKind.USER,
                    text=_clip(content),
                    ts=ts,
                    raw_index=index,
                )
            ]
        out: list[Event] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                body = block.get("content")
                out.append(
                    Event(
                        kind=EventKind.TOOL_RESULT,
                        text=_clip(
                            body if isinstance(body, str) else json.dumps(body, default=str)
                        ),
                        ts=ts,
                        raw_index=index,
                    )
                )
            elif block.get("type") == "text":
                out.append(
                    Event(
                        kind=EventKind.USER,
                        text=_clip(block.get("text")),
                        ts=ts,
                        raw_index=index,
                    )
                )
        return out

    def native_children(self, transcript: Path) -> list[NativeChild]:
        """Sidechain records, deduplicated by the uuid that roots each one."""
        seen: set[str] = set()
        out: list[NativeChild] = []
        try:
            with transcript.open(encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict) or not record.get("isSidechain"):
                        continue
                    # Sidechain records have no id of their own beyond the
                    # chain they hang off; the root is the first one whose
                    # parentUuid is absent.
                    root = record.get("parentUuid") or record.get("uuid")
                    if not root or root in seen:
                        continue
                    seen.add(root)
                    out.append(NativeChild(session_id=root, agent="task"))
        except OSError:
            return []
        return out

    def is_idle_screen(self, capture: str) -> bool:
        """Claude Code shows a `>` prompt when waiting for input.

        When idle, the last non-empty line of the rendered pane is just
        the prompt character. When working, the last line is agent
        output. When a human is typing, the last line has text after
        the prompt.
        """
        return last_screen_line(capture) in IDLE_PROMPTS

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify the screen as `trust`, `approval`, `working`, `prompt` or `unknown`.

        Trust first: the workspace-trust onboarding dialog has no
        ``Esc to cancel`` footer (it predates the approval chrome) and no
        status footer, so without a dedicated arm it falls through to
        ``UNKNOWN`` — the reducer leaves the status untouched and the send
        gate lets keystrokes through into a trust prompt. That is
        safety-critical: injecting Enter into a trust dialog accepts trust
        no human granted.

        Approval next: the dialog's footer chrome (`Esc to cancel`) is a
        frame element the CLI draws, not text the agent produced, so it cannot
        appear in echoed output. The dialog replaces the status footer, so all
        three markers are mutually exclusive on a real capture — the order is
        what keeps that from mattering if a future release overlaps them.

        Working before prompt because the reducer maps `prompt` to IDLE: a
        working screen misread as a prompt does not merely mislabel it, it
        also lets `_rescue_jobs` finish the agent's jobs mid-turn, which
        resolves the caller's `await` on a turn that never ended.

        Both are read from the status footer rather than the last line: a real
        capture draws the footer below the prompt, so the prompt is never the
        last non-blank line and `is_idle_screen` does not fire on a real
        screen.
        """
        if TRUST_MARKER in capture:
            return ScreenReading(
                kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH
            )
        if APPROVAL_MARKER in capture:
            return ScreenReading(
                kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH
            )
        lines = [line for line in capture.splitlines() if line.strip()]
        tail = lines[-_SCREEN_TAIL_LINES:]
        if any(WORKING_MARKER in line for line in tail):
            return ScreenReading(
                kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH
            )
        if any(IDLE_FOOTER in line for line in tail):
            return ScreenReading(
                kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH
            )
        return ScreenReading(
            kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW
        )


#: What the loader looks for. An instance, not the class: see
#: docs/harness-plugins.md. Shipped adapters meet the same contract as anything
#: dropped in $THEATER_HOME/harnesses, which is the point of shipping them here.
HARNESS = ClaudeCodeHarness()
