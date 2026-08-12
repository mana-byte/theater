"""OpenAI Codex CLI.

Launch lever
------------
`-c key=value` sets a config override, and the value is parsed as TOML, so the
MCP server is registered by writing three dotted keys inline:

    -c mcp_servers.theater.command="…"  -c mcp_servers.theater.args=["mcp",…]

Verified with `codex mcp list` inside a launched session. This is an *override*
on top of ~/.codex/config.toml rather than a replacement, so the user's own
servers survive — same policy as the other two adapters.

Approval flags are always passed in pairs (`-a` with `-s`). Codex has two
independent axes — approval policy and sandbox — and with neither flag it
inherits whatever the user put in ~/.codex/config.toml, which may well be
`never` / `danger-full-access`. Theater's approval mode is a promise to the
caller of `spawn`, so it must not be inheritable.

The first-launch trust dialog
-----------------------------
On the first launch inside a directory that is not listed under
`[projects."<path>"] trust_level = "trusted"` in ~/.codex/config.toml, codex
shows a modal asking whether you trust the directory, and nothing runs until a
human answers. Tested and unable to suppress: `-a untrusted -s read-only`,
`--dangerously-bypass-approvals-and-sandbox`, and both spellings of a
`-c projects."…".trust_level="trusted"` override. Two ways out were considered
and rejected — writing the trust entry into the user's config (Theater does not
own that file) and pointing CODEX_HOME elsewhere (loses auth.json, the user's
MCP servers, and session history). So a spawn into a fresh directory sits at the
dialog until someone answers it once. `is_idle_screen` reports that pane as
awaiting input, which is exactly right.

Transcript layout
-----------------
    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<local-ISO>-<session_id>.jsonl

Two independent traps in that name. The timestamp is *local* time while every
`timestamp` field inside the file is UTC, so the two are never comparable — time
filtering here goes through stat() and nothing else. The uuid suffix, on the
other hand, is exactly `session_meta.payload.session_id` (checked on every
transcript to hand), which makes a known session id a pure glob.

Record shape
------------
One JSON record per line, `{timestamp, type, payload}`, discriminated on
`payload.type`. The turn boundary is `task_complete`, and its
`last_agent_message` repeats the final `agent_message` verbatim — which is why
the `final_answer` phase is dropped and the text is taken from the boundary
record instead. The observer hands the *turn-ending* event's text back to
whoever is awaiting the job (observer `_answer_turn`), so a boundary event with
no text would resolve the send with an empty result — the observer falls back
to the turn's last assistant text for exactly this shape, but a boundary that
carries its own text is better than relying on that.

`turn_aborted` (a human pressing esc) also ends the turn. It has to: 8
`task_started` records across the sampled transcripts closed as 5
`task_complete` plus 3 `turn_aborted`, and treating the aborts as non-terminal
would leave a caller awaiting a reply that is never coming.
"""

from __future__ import annotations

import json
import re
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
    theater_binary,
)
from theater.harness.observation import TranscriptObserver
from theater.models import BadRequest

#: The composer prompt. A single glyph (U+203A), not the ASCII ">" that Claude
#: Code uses.
PROMPT = "\u203a"

#: Present in the status bar for as long as a turn is running. Codex keeps a
#: persistent footer under the composer, so unlike the other two harnesses the
#: bottom line is never the prompt and `last_screen_line` cannot be used.
WORKING_MARKER = "esc to interrupt"

#: How far up from the bottom to look for the composer. The footer is one line,
#: but a multi-line composer or a notice above it can push the prompt further
#: up; beyond this the pane is showing something else entirely.
_SCREEN_TAIL_LINES = 5

#: `session_meta` is the first record and carries `cwd`, so a candidate is
#: probed by reading exactly one line. Observed at 18-22 KB (the payload embeds
#: the whole system prompt), so the cap only exists to stop a pathological file
#: from being read into memory.
_CWD_PROBE_BYTES = 256 * 1024

#: The filename is `rollout-<local ISO with - separators>-<uuid>`. Anchoring on
#: the fixed-width timestamp is what lets the uuid keep its own hyphens.
_STEM = re.compile(r"^rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(.+)$")


def _epoch(value) -> float | None:
    """Codex writes ISO-8601 with a Z suffix, same as Claude Code."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _flatten(output) -> str:
    """Tool output is a list of `{"type": "input_text", "text": …}` blocks."""
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return "" if output is None else json.dumps(output, default=str)
    parts = []
    for block in output:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


class CodexHarness(Harness):
    name = "codex"
    binary = "codex"
    #: A filled ring. Deliberately not another asterisk-family glyph: `✻` is
    #: taken by Claude Code and the near-neighbours (`✳ ❋ ✺`) are hard to tell
    #: apart at one column.
    icon = "\u25c9"
    #: What an agent might call itself at registration. A spelling that does not
    #: normalize is observed as nothing at all, so these are not cosmetic.
    aliases = ("codex-cli", "codex_cli", "openai-codex", "Codex")

    def __init__(self, root: Path | None = None):
        #: `root` locates the transcript, which is the observer's
        #: business alone; nothing about launching depends on it.
        self.observer = CodexObserver(root=root)

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
        command = json.dumps(theater_binary())
        args = json.dumps(["mcp", "--id", participant_id])
        argv = [
            "codex",
            "-c",
            f"mcp_servers.{SERVER_NAME}.command={command}",
            "-c",
            f"mcp_servers.{SERVER_NAME}.args={args}",
        ]
        if approval == "yolo":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        elif approval == "edits":
            argv += ["-a", "on-request", "-s", "workspace-write"]
        else:
            argv += ["-a", "untrusted", "-s", "read-only"]
        if prompt:
            # Positional, and it auto-submits: the session comes up already
            # working, with no keystroke injection.
            argv.append(prompt)
        return LaunchPlan(argv=argv)


class CodexObserver(TranscriptObserver):
    """Read `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.

    Note the date directories: rollout files are filed under the day the
    session started, in UTC, which is not the local date for most of the world
    for part of every day.
    """

    def __init__(self, root: Path | None = None):
        #: Injectable so tests never touch the real ~/.codex.
        self.root = root or Path.home() / ".codex" / "sessions"

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
            # The uuid suffix of the filename is the session id, so this is an
            # exact lookup: no scan, and no need to guess the date directory.
            hit = next(self.root.glob(f"*/*/*/rollout-*-{session_id}.jsonl"), None)
            if hit is not None:
                return hit
        want = str(Path(cwd).resolve()) if cwd else None
        if want is None:
            return None
        candidates = []
        for path in self.root.glob("*/*/*/rollout-*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            if after is not None:
                # stat, never the filename: that timestamp is local time with
                # no offset recorded, and the caller's floor is a unix epoch.
                born = getattr(st, "st_birthtime", st.st_ctime)
                if born < after:
                    continue
            candidates.append((st.st_mtime, path))
        for _, path in sorted(candidates, reverse=True):
            if self._transcript_cwd(path) == want:
                return path
        return None

    def _transcript_cwd(self, path: Path) -> str | None:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                line = fh.readline(_CWD_PROBE_BYTES)
        except OSError:
            return None
        try:
            record = json.loads(line)
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get("type") != "session_meta":
            return None
        payload = record.get("payload")
        found = payload.get("cwd") if isinstance(payload, dict) else None
        return str(Path(found).resolve()) if found else None

    def session_id(self, transcript: Path) -> str | None:
        """The uuid tail of the filename. Verified against session_meta."""
        found = _STEM.match(transcript.stem)
        return found.group(1) if found else None

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
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return []

        ts = _epoch(record.get("timestamp"))
        kind = record.get("type")
        if kind == "event_msg":
            return self._event(payload, ts, index, clip_text=clip_text)
        if kind == "response_item":
            return self._item(payload, ts, index, clip_text=clip_text)
        # session_meta, turn_context, world_state: bookkeeping.
        return []

    def _event(
        self, payload: dict, ts: float | None, index: int, *, clip_text: bool
    ) -> list[Event]:
        _clip = clipper(clip_text)
        ptype = payload.get("type")

        if ptype == "user_message":
            return [
                Event(
                    kind=EventKind.USER,
                    text=_clip(payload.get("message")),
                    ts=ts,
                    raw_index=index,
                )
            ]
        if ptype == "agent_message":
            if payload.get("phase") == "final_answer":
                # Repeated verbatim by the task_complete that follows it.
                # Emitting both would double every reply on the bus.
                return []
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=_clip(payload.get("message")),
                    ts=ts,
                    raw_index=index,
                )
            ]
        if ptype == "task_complete":
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=_clip(payload.get("last_agent_message")),
                    ts=ts,
                    turn_end=True,
                    raw_index=index,
                )
            ]
        if ptype == "turn_aborted":
            return [
                Event(
                    kind=EventKind.ERROR,
                    text=f"turn aborted: {payload.get('reason') or 'unknown'}",
                    ts=ts,
                    turn_end=True,
                    raw_index=index,
                )
            ]
        if ptype in ("mcp_tool_call_begin", "mcp_tool_call_end"):
            # The only visibility into MCP use, Theater's own tools included:
            # these calls never appear as response_items.
            invocation = payload.get("invocation")
            invocation = invocation if isinstance(invocation, dict) else {}
            tool_name = ".".join(
                str(part)
                for part in (invocation.get("server"), invocation.get("tool"))
                if part
            )
            if ptype == "mcp_tool_call_begin":
                return [
                    Event(
                        kind=EventKind.TOOL_CALL,
                        tool_name=tool_name or None,
                        ts=ts,
                        raw_index=index,
                    )
                ]
            return [
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=_clip(self._mcp_result(payload.get("result"))),
                    tool_name=tool_name or None,
                    ts=ts,
                    raw_index=index,
                )
            ]
        # token_count, task_started, patch_apply_end, thread_settings_applied:
        # progress accounting, not conversation.
        return []

    def _mcp_result(self, result) -> str:
        """Unwrap the Rust-style `{"Ok"|"Err": …}` an MCP call comes back as."""
        if not isinstance(result, dict):
            return "" if result is None else json.dumps(result, default=str)
        ok = result.get("Ok")
        if isinstance(ok, dict):
            return _flatten(ok.get("content"))
        err = result.get("Err")
        if err is not None:
            return err if isinstance(err, str) else json.dumps(err, default=str)
        return json.dumps(result, default=str)

    def _item(
        self, payload: dict, ts: float | None, index: int, *, clip_text: bool
    ) -> list[Event]:
        _clip = clipper(clip_text)
        ptype = payload.get("type")

        if ptype in ("custom_tool_call", "function_call"):
            return [
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=payload.get("name"),
                    ts=ts,
                    raw_index=index,
                )
            ]
        if ptype in ("custom_tool_call_output", "function_call_output"):
            # No tool name: the record carries only `call_id`, and resolving it
            # would mean holding state across lines, which parse() does not do.
            return [
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=_clip(_flatten(payload.get("output"))),
                    ts=ts,
                    raw_index=index,
                )
            ]
        # `message` duplicates the event_msg stream and `reasoning` is the
        # agent's private thinking; both are dropped, as in the Claude adapter.
        return []

    def native_children(self, transcript: Path) -> list[NativeChild]:
        """Codex has no sub-agent mechanism of its own."""
        return []

    def is_idle_screen(self, capture: str) -> bool:
        """Codex keeps a status footer below the composer.

        So the bottom line is never the prompt and `last_screen_line` — which
        both other adapters use — would never match. Instead: a running turn
        always renders `esc to interrupt`, and an idle one renders a composer
        line starting with `›` somewhere in the last few lines.

        The composer shows greyed-out placeholder text when empty ("Explain
        this codebase"), and a colourless capture cannot tell that apart from
        a human's half-typed message. That is tolerable because this method
        only feeds the AWAITING_INPUT display hint; whether a human is present
        is decided separately, from `pane_in_mode`, and never from a scrape.

        The first-launch trust dialog also reads as idle here. It genuinely is:
        nothing proceeds until someone answers it.
        """
        if WORKING_MARKER in capture:
            return False
        lines = [line.strip() for line in capture.splitlines() if line.strip()]
        return any(line.startswith(PROMPT) for line in lines[-_SCREEN_TAIL_LINES:])


#: What the loader looks for. An instance, not the class: see
#: docs/harness-plugins.md. Shipped adapters meet the same contract as anything
#: dropped in $THEATER_HOME/harnesses, which is the point of shipping them here.
HARNESS = CodexHarness()
