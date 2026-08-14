"""Mistral Vibe.

Launch lever
------------
`$VIBE_MCP_SERVERS`: any VIBE_* env var overrides the matching config field,
and mcp_servers is union-merged by `name` (vibe_schema.py:321), so the user's
other servers survive and only `theater` is replaced. The variable is read by
the *harness*, whose own environment we do control via `tmux new-window -e` —
which is why the allowlist that blocks the environment channel for the MCP
server itself does not apply here.

Transcript layout
-----------------
    ~/.vibe/logs/session/session_<YYYYMMDD>_<HHMMSS>_<short>/messages.jsonl
                                                            /meta.json

`short` is the first 8 characters of the session id, so a known session id
narrows to a single directory by glob. Otherwise the working directory has to
come from meta.json, which also holds the session's own record of the
sub-agents it spawned.

Sub-agent sessions live *under* their parent's directory (`agents/<name>_...`),
so globbing `session_*` at the root deliberately finds only top-level sessions.

Record shape
------------
Three roles: user, assistant, tool. The turn boundary is the *absence* of the
`tool_calls` key on an assistant record — observed absent 2, present 64, and
never null or empty across a sampled transcript, but read defensively anyway
since falsy and absent should mean the same thing here.

Vibe writes no timestamps. Not "sometimes", not "in a different field": there
is no time information in messages.jsonl at all. Events from this harness carry
ts=None and the observer stamps its own observation time, which is a different
quantity and is labelled as such.
"""

from __future__ import annotations

import json
import os
import tomllib
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
from theater.harness.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
    TranscriptObserver,
)
from theater.models import BadRequest

#: Screen lines that mean "waiting for you". Vibe's prompt is `❯` (U+276F);
#: the variants cover trailing space and the boxed form. Anything after the
#: prompt is someone typing, which is presence, not idleness — so these must
#: stay exact matches.
IDLE_PROMPTS = ("❯", "❯ ", "> ❯")

#: Prompt forms recognised in a tail scan by `screen_reading`. A superset of
#: `IDLE_PROMPTS` because a real capture can render the prompt as bare `>`
#: (ASCII), which the exact `IDLE_PROMPTS` tuple used by `is_idle_screen` does
#: not include. Seen in `tests/fixtures/screens/vibe_idle.txt`.
_SCREEN_IDLE_PROMPTS = (*IDLE_PROMPTS, ">")

#: Drawn by the CLI at the bottom of every permission box, regardless of which
#: tool or question the box is about. Chosen over the question text
#: (`Permission for the`) because `Esc reject` is frame furniture — the CLI's
#: own navigation hint — and cannot appear in the agent's echoed output.
#: Present in `tests/fixtures/screens/vibe_approval.txt`.
#:
#: Vibe renders its working spinner (`Esc/Ctrl+C to interrupt`) *and* the
#: permission box simultaneously, so approval must be tested before the working
#: marker or every approval dialog is misclassified as `working`.
#:
#: Case matters: vibe's picker footers render `Esc Cancel`, `Esc Close`,
#: `Esc Back`, `Esc exit`, and `Esc cancel` (lowercase) — all user-initiated
#: menus that must NOT be classified as approval. Only the lowercase `reject`
#: is the permission box.
APPROVAL_MARKER = "Esc reject"

#: Drawn in the spinner line while a turn is in flight. Vibe has two spellings
#: of the spinner hint (`loading.py:170-177`):
#:   plain:   `(9m45s Esc/Ctrl+C to interrupt)`
#:   queued:  `(1m23s Esc to interrupt · Ctrl+C to cancel last queued message)`
#: The substring `to interrupt` matches both spellings, but it is also
#: ordinary English prose that an agent can echo in its own output, so it
#: cannot be used alone.
#:
#: `WORKING_MARKER_KEY` is the other token that identifies the spinner line.
#: Both spellings put `Esc` and `to interrupt` on the SAME line, and ordinary
#: prose containing "to interrupt" — e.g. "I added a signal handler to
#: interrupt the loop" — does not also contain `Esc` on that line.
#:
#: The safety of the loose substring rests on TWO things together: the tail
#: window (`_SPINNER_TAIL_LINES`) and the `Esc` co-occurrence requirement.
#: Neither alone is enough, because the tail window unavoidably contains the
#: agent's closing lines of output — the spinner renders directly above the
#: composer, and the agent's closing sentences render directly above the
#: spinner. Do not separate any of the three: the substring, the key, and the
#: tail window.
#:
#: Codex's working arm uses an endswith anchor on its spinner line, but that
#: does NOT work here: the plain hint ends with `)` and the queued hint ends
#: with `message)`, so anchoring on the end of the line would break both. The
#: two-token co-occurrence test is the right discriminator for vibe.
WORKING_MARKER = "to interrupt"

#: The second token that must co-occur with `WORKING_MARKER` on the same tail
#: line. See `WORKING_MARKER` for why both are required.
WORKING_MARKER_KEY = "Esc"

#: Rendered by the workspace-trust dialog (`trust_folder_dialog.py:95-97`),
#: which runs at startup when vibe detects config files in an untrusted folder
#: (`startup.py:127-147`). The warning paragraph starts with this fragment on
#: its own rendered line, so it survives Textual's line wrapping. Chosen over
#: the title (`Trust this folder?` / `Trust folder or repository?`) because the
#: title varies by context, and over the button labels (`Don't trust` etc.)
#: because the warning is unique to this dialog — a button label like
#: `Don't trust` could in principle appear in echoed output, whereas
#: `Malicious configs can modify` is prose that only this screen renders.
#: Unlike `WORKING_MARKER`, this stays a whole-capture match: the trust dialog
#: only appears at startup when there is no agent output on the pane, and the
#: marker is body text in the middle of the dialog, not footer chrome, so
#: tail-scoping would break it.
TRUST_MARKER = "Malicious configs can modify"

#: How far up from the bottom to look for the prompt. A real capture has a
#: separator and a cwd/token footer below the prompt, so the prompt is not
#: the last non-blank line and `is_idle_screen` — which checks only the last
#: line — does not fire on a real screen.
_SCREEN_TAIL_LINES = 6

#: How far up from the bottom to scan for the working spinner. Measured in
#: both `vibe_working.txt` and `vibe_working_queued.txt` the spinner sits 6
#: lines above the bottom of the capture (separator, composer prompt, blank,
#: blank, separator, cwd/token footer). The queued hint is longer and may wrap
#: on a narrow terminal, adding a line or two, so 8 leaves a 2-line margin
#: without reaching into the agent's output area.
_SPINNER_TAIL_LINES = 8

#: How many session directories to inspect when searching by working directory.
#: They are scanned newest first and a live session is always near the top, so
#: this bounds the cost of a home directory with thousands of old sessions.
_SCAN_LIMIT = 200


def _in_screen_tail(capture: str, markers: tuple[str, ...], limit: int) -> bool:
    """Whether some tail line contains every marker in `markers`.

    The spinner and footer chrome always render at the bottom of the pane, and
    matching the whole pane lets agent output impersonate chrome — e.g. the
    phrase ``to interrupt`` is ordinary English that an agent can echo. Scoping
    to the tail is necessary but not sufficient: the tail also contains the
    agent's closing lines. Requiring a second token (``Esc``) on the same line
    is what distinguishes the spinner from prose.
    """
    lines = capture.splitlines()
    return any(
        all(m in line for m in markers) for line in lines[-limit:] if line
    )


class VibeHarness(Harness):
    name = "vibe"
    binary = "vibe"
    #: Stacked bars, echoing the Mistral mark. A lozenge or an "M" would both
    #: collide with the asterisk-family glyphs a third harness is likely to want.
    icon = "▤"
    #: What an agent might call itself at registration. A spelling that does not
    #: normalize is observed as nothing at all, so these are not cosmetic.
    aliases = ("Vibe", "mistral-vibe", "mistral_vibe")

    def __init__(self, root: Path | None = None):
        #: `root` is only the observer's business — nothing about launching vibe
        #: depends on where it writes — so it is passed straight through rather
        #: than stored here as well.
        self.observer = VibeObserver(root=root)

    # ---- launching ------------------------------------------------------

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
    ) -> LaunchPlan:
        if approval not in APPROVALS:
            raise BadRequest(
                f"approval must be one of {', '.join(APPROVALS)}, got {approval!r}"
            )
        servers = [
            {
                "name": SERVER_NAME,
                "transport": "stdio",
                "command": theater_binary(),
                "args": ["mcp", "--id", participant_id],
            }
        ]
        argv = ["vibe"]
        if approval == "yolo":
            argv.append("--yolo")
        elif approval == "edits":
            argv += ["--agent", "accept-edits"]
        if prompt:
            argv.append(prompt)
        env = {"VIBE_MCP_SERVERS": json.dumps(servers)}
        # The only shipped harness with no `--model` flag: the same VIBE_*
        # override mechanism that carries the MCP server carries the model.
        #
        # It is set unconditionally, empty when no model was asked for, because
        # an environment variable is inherited and a flag is not. A vibe agent
        # spawned with a model would otherwise pass it down to every descendant
        # that did not name one, and the child would come up on a model nobody
        # chose — a bug that is invisible until the bill arrives. Empty means
        # "use the configured default", which is what an unset variable means.
        env["VIBE_ACTIVE_MODEL"] = model or ""
        return LaunchPlan(argv=argv, env=env)

    def discover_models(self) -> list[str]:
        """Read `[[models]]` out of vibe's own config.

        Vibe has no `models` subcommand, but its model set is not a remote
        catalogue — it is a list the user already wrote in `config.toml`, which
        makes it exactly the thing worth copying into Theater's `[models]`.

        Both spellings are returned. `VIBE_ACTIVE_MODEL` accepts either the
        `name` (`claude-opus-5`) or the shorter `alias` (`opus-5`), and which
        one someone wants to see in Theater's config is a matter of taste.

        Reading another tool's config file is a coupling Theater does not
        otherwise take, and it is only tolerable because of where it sits:
        discovery, run by hand, printed for review, never on the spawn path. A
        vibe release that renames these keys degrades this to "found nothing"
        rather than breaking a spawn.
        """
        path = self._config_path()
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise NotImplementedError(f"{path} does not exist") from exc
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise NotImplementedError(f"{path} cannot be read: {exc}") from exc

        entries = raw.get("models")
        if not isinstance(entries, list):
            raise NotImplementedError(f"{path} has no [[models]] entries")

        found: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for key in ("name", "alias"):
                value = entry.get(key)
                if isinstance(value, str) and value and value not in found:
                    found.append(value)
        return found

    @staticmethod
    def _config_path() -> Path:
        """Where vibe keeps its config. `$VIBE_HOME` wins, as it does for vibe."""
        home = os.environ.get("VIBE_HOME")
        base = Path(home) if home else Path.home() / ".vibe"
        return base / "config.toml"


class VibeObserver(TranscriptObserver):
    """Read `~/.vibe/logs/session/*/messages.jsonl`.

    Vibe is the harness that rotates: a new session directory appears on some
    turns, so the transcript this observer is reading can stop growing while the
    agent is very much alive. `TranscriptSource.refresh` handles it by searching
    on cwd alone; see the note there.
    """

    def __init__(self, root: Path | None = None):
        #: Injectable so tests never touch the real ~/.vibe.
        self.root = root or Path.home() / ".vibe" / "logs" / "session"

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
            short = session_id.split("-")[0][:8]
            for d in sorted(self.root.glob(f"session_*_{short}"), reverse=True):
                messages = d / "messages.jsonl"
                if messages.exists():
                    return messages
        want = str(Path(cwd).resolve()) if cwd else None
        if want is None:
            return None
        seen = 0
        # Directory names start with a fixed-width UTC timestamp, so reverse
        # lexicographic order is newest first without parsing anything.
        for d in sorted(self.root.glob("session_*"), reverse=True):
            messages = d / "messages.jsonl"
            if not messages.exists():
                continue
            if after is not None:
                try:
                    st = d.stat()
                except OSError:
                    continue
                # Stat, not the name: the name's timestamp has no timezone
                # marker and the caller's floor is a unix epoch.
                if getattr(st, "st_birthtime", st.st_ctime) < after:
                    continue
            seen += 1
            if seen > _SCAN_LIMIT:
                return None
            if self._meta_cwd(d) == want:
                return messages
        return None

    def _meta(self, session_dir: Path) -> dict:
        try:
            data = json.loads((session_dir / "meta.json").read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _meta_cwd(self, session_dir: Path) -> str | None:
        env = self._meta(session_dir).get("environment") or {}
        found = env.get("working_directory") if isinstance(env, dict) else None
        return str(Path(found).resolve()) if found else None

    def session_id(self, transcript: Path) -> str | None:
        """meta.json is authoritative; the directory suffix is only 8 chars."""
        found = self._meta(transcript.parent).get("session_id")
        if found:
            return str(found)
        name = transcript.parent.name
        return name.rsplit("_", 1)[-1] if name.startswith("session_") else None

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

        _clip = clipper(clip_text)

        role = record.get("role")
        if role == "user":
            return [
                Event(
                    kind=EventKind.USER,
                    text=_clip(record.get("content")),
                    raw_index=index,
                )
            ]
        if role == "tool":
            return [
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=_clip(record.get("content")),
                    tool_name=record.get("name"),
                    raw_index=index,
                )
            ]
        if role != "assistant":
            return []

        calls = record.get("tool_calls") or []
        out: list[Event] = []
        content = record.get("content")
        if content:
            out.append(
                Event(kind=EventKind.ASSISTANT, text=_clip(content), raw_index=index)
            )
        for call in calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            out.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=fn.get("name") if isinstance(fn, dict) else None,
                    raw_index=index,
                )
            )
        if calls:
            return out
        # No tool calls: the agent has finished its turn. Content can still be
        # None on a degenerate record, so guarantee the boundary event exists.
        #
        # No turn_id: the records carry no id of any kind, not even a message
        # id, so there is nothing honest to put there. Leaving it None means
        # the observer treats every Vibe boundary as its own turn — correct
        # here, because Vibe writes one record per boundary and never repeats
        # one. Synthesising an id from the index would be a lie the moment a
        # record is re-read after a relocate.
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
                Event(kind=EventKind.ASSISTANT, turn_end=True, raw_index=index)
            )
        return out

    def native_children(self, transcript: Path) -> list[NativeChild]:
        """Read the session's own list of sub-agents from meta.json."""
        entries = self._meta(transcript.parent).get("child_sessions") or []
        out: list[NativeChild] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("session_id"):
                continue
            out.append(
                NativeChild(
                    session_id=entry["session_id"],
                    agent=entry.get("agent"),
                    relative_path=entry.get("relative_path"),
                    tool_call_id=entry.get("tool_call_id"),
                )
            )
        return out

    def is_idle_screen(self, capture: str) -> bool:
        """Vibe shows a bare `❯` prompt when waiting for input.

        The capture-pane output ends with the current input line. If the
        last non-empty line is just the prompt symbol (with optional
        whitespace), the agent is idle. If there is text after the prompt,
        someone is typing — but that's human presence, not idle. If the
        last line is agent output, the agent is still rendering.
        """
        return last_screen_line(capture) in IDLE_PROMPTS

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify the screen as `trust`, `approval`, `working`, `prompt` or `unknown`.

        Order is load-bearing here, twice over, because vibe keeps drawing the
        composer and the spinner underneath its permission box:

        Trust before everything: the trust dialog runs at startup, before any
        turn or prompt, and is a modal that blocks all interaction — including
        the send gate. It must win over any other marker.

        Approval before working, because `WORKING_MARKER` is on screen in the
        same capture as the permission box — check working first and every
        dialog reads as `working`, so AWAITING_INPUT is never reachable.

        Working before prompt, because the composer's empty prompt line stays
        on screen during a turn. Reading a working screen as a prompt does not
        merely mislabel it: the reducer maps `prompt` to IDLE, and
        `_rescue_jobs` then finishes the agent's jobs mid-turn, resolving the
        caller's `await` on a turn that never ended.

        The prompt is found by scanning the tail rather than checking only the
        last line: a real capture has a separator and a cwd/token footer below
        it, so `is_idle_screen` does not fire on a real screen.
        """
        if TRUST_MARKER in capture:
            return ScreenReading(
                kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH
            )
        if APPROVAL_MARKER in capture:
            return ScreenReading(
                kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH
            )
        if _in_screen_tail(
            capture, (WORKING_MARKER, WORKING_MARKER_KEY), _SPINNER_TAIL_LINES
        ):
            return ScreenReading(
                kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH
            )
        lines = [line.strip() for line in capture.splitlines() if line.strip()]
        if any(line in _SCREEN_IDLE_PROMPTS for line in lines[-_SCREEN_TAIL_LINES:]):
            return ScreenReading(
                kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH
            )
        return ScreenReading(
            kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW
        )


#: What the loader looks for. An instance, not the class: see
#: docs/harness-plugins.md. Shipped adapters meet the same contract as anything
#: dropped in $THEATER_HOME/harnesses, which is the point of shipping them here.
HARNESS = VibeHarness()
