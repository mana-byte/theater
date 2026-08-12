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
turn_end events; harmless, since the derived status is idempotent.

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
    Harness,
    LaunchPlan,
    NativeChild,
    clipper,
    last_screen_line,
    theater_binary,
)
from theater.models import BadRequest

#: Screen lines that mean "waiting for you". Anything after the prompt is
#: someone typing, which is presence, not idleness — so these stay exact.
IDLE_PROMPTS = (">", "> ")

#: Records to read before giving up on finding a `cwd` in a candidate
#: transcript. The first record is a `permission-mode` entry that has none;
#: `cwd` first appears around index 2.
_CWD_PROBE_RECORDS = 20

#: Individual records can be hundreds of KB. Bound the probe read so scanning
#: candidates never turns into reading whole transcripts.
_CWD_PROBE_BYTES = 256 * 1024


def _epoch(value) -> float | None:
    """Claude Code writes ISO-8601 with a Z suffix."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ClaudeCodeHarness(Harness):
    name = "claude"
    binary = "claude"
    #: Claude Code prints this same spoked asterisk as its own spinner glyph,
    #: so it reads as the product's mark rather than an arbitrary bullet.
    icon = "✻"

    def __init__(self, root: Path | None = None):
        #: Injectable so tests never touch the real ~/.claude.
        self.root = root or Path.home() / ".claude" / "projects"

    # ---- launching ------------------------------------------------------

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
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

    # ---- observing ------------------------------------------------------

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
                        raw_index=index,
                    )
                )
            elif btype == "tool_use":
                out.append(
                    Event(
                        kind=EventKind.TOOL_CALL,
                        tool_name=block.get("name"),
                        ts=ts,
                        raw_index=index,
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
                    raw_index=last.raw_index,
                )
            else:
                out.append(
                    Event(
                        kind=EventKind.ASSISTANT, ts=ts, turn_end=True, raw_index=index
                    )
                )
        return out

    def _user(self, message: dict, ts: float | None, index: int, *, clip_text: bool = True) -> list[Event]:
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
                for line in fh:
                    line = line.strip()
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
