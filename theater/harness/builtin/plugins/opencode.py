"""OpenCode.

The first adapter whose output is not a file. It writes nothing per session:
everything lands in one SQLite database shared by every session on the machine,
so there is no transcript to tail, no byte offset to hold, and the four methods
the other three adapters are made of have nothing to say. What it has instead is
an append-only `event` table with a per-session monotonic `seq`, which is a
better cursor than a byte offset — hence `open_source` and `OpenCodeSource`
below, and hence commit 1 of this release.

Launch lever
------------
`$OPENCODE_CONFIG` points at a config file that is **merged** into the user's
own rather than replacing it. Verified with `opencode debug config`: the user's
model, providers and seven existing MCP servers all survived, with our `theater`
entry added. So the same policy as the other three adapters holds — we register
one server and touch nothing else — and, importantly, we never write to
`~/.config/opencode/opencode.jsonc`, which is JSONC and would lose every comment
in it to a programmatic rewrite.

`opencode mcp list` inside a session launched this way reports `theater ✓
connected`. The participant id is baked into the command argv, for the reason in
base.py: the MCP SDK drops the parent environment.

Approval modes
--------------
`--auto` is yolo and there is no flag between that and the default. `edits`
therefore degrades to `manual` — the same prompts, no fewer. The alternative was
to emit a `permission` block into the merged config, which was rejected: a key
this adapter has not verified against the running version would take the whole
launch down with it, and a spawn that will not start is worse than a spawn that
asks twice.

Where the output goes
---------------------
    $XDG_DATA_HOME/opencode/opencode-stable.db   (~/.local/share by default)

Four tables matter. `session(id, parent_id, directory, time_created)` locates a
session: `directory` is the *resolved* path, which on macOS means `/private/var`
where the caller says `/var`, so both sides get `Path.resolve()`. `parent_id IS
NULL` excludes sub-agent sessions, which share their parent's directory and
would otherwise be picked up as the newest match. `event(aggregate_id, seq,
type, data)` is the live feed. `message` and `part` hold current state and are
what `history()` reads. Every timestamp in the database is milliseconds.

The database is read `mode=ro` and never through `opencode db`, which takes a
write lock and fails with "database is locked" while a session is running. Note
`immutable=1` is deliberately *not* set: it promises the file will not change,
which is the opposite of what a tail needs.

Event shapes
------------
`message.updated.1` carries `data.info` — id, role, `finish`, `time`. It fires
twice at the end of a message, once with `finish` and once again with
`time.completed`, so finishes are deduped by message id.

`finish == "tool-calls"` ends a *step*, not a turn: a new assistant message
follows immediately. The turn boundary is a finish that is anything else
(`stop`, in every trace sampled). Getting this wrong would resolve a caller's
`await_sessions` at the first tool call with an empty answer.

`message.part.updated.1` carries `data.part` — text, tool, step-start or
step-finish — and `data.time`, the event's own millisecond stamp. Text parts
arrive empty and are then *replaced* by the complete text, not appended to.
Tool parts move pending -> running -> completed, with `state.input` empty until
running and `state.output` set at completed.

Assistant text is therefore buffered and emitted as one event when its message
finishes, rather than streamed. Streaming would put the same reply on the bus
several times over, each a prefix of the last, and hand `_answer_turn` whichever
fragment happened to land last. The cost is that a reply appears at
the end of its step rather than as it is typed; tool activity still streams, so
a long turn is not silent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Literal

from theater import paths
from theater.harness.base import (
    APPROVALS,
    SERVER_NAME,
    Event,
    EventKind,
    EventPath,
    Harness,
    LaunchPlan,
    clip,
    theater_binary,
    whole,
)
from theater.harness.observation import (
    HarnessObserver,
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.source import Attachment, Batch, History, Source, TranscriptCandidate
from theater.models import BadRequest, Status
from theater.provenance import (
    TranscriptProvenance,
    is_trusted_provenance,
    normalize_provenance,
)

logger = logging.getLogger("theater.harness.opencode")

#: Reported by `opencode debug paths` as the data directory's contents. The
#: `-stable` suffix is the release channel; a nightly writes its own file, and
#: pointing at the wrong one finds no sessions at all rather than failing loudly.
DB_NAME = "opencode-stable.db"

#: Seconds `opencode models` gets before `theater models --discover` gives up.
#: It reaches the network, but a human is waiting at a terminal — long enough
#: for a slow link, short enough to fail rather than hang.
MODELS_TIMEOUT = 20

#: The two spellings of the working footer, rendered by the Prompt component
#: (`component/prompt/index.tsx:1587-1592`). The footer draws `esc ` followed
#: by `"interrupt"` normally and `"again to interrupt"` after one Esc press.
#: Both spellings must be matched.
#:
#: Do NOT shorten to `"interrupt"`: `routes/session/index.tsx:1569` renders
#: `· interrupted` in the message log after an abort, and `"interrupt"` is a
#: substring of `"interrupted"` — an idle pane after an abort would read WORKING
#: forever. Neither full spelling is a substring of `· interrupted`.
#:
#: These are footer chrome, not body text, so a bare containment test lets
#: agent output impersonate chrome (an agent working on THIS repo prints
#: `esc interrupt` in its own output). Matched only inside `_in_screen_tail`
#: with a co-occurrence guard, not by whole-capture containment.
WORKING_MARKERS = ("esc interrupt", "again to interrupt")

#: The idle footer's right-hand hint. Keybinding-derived: `ctrl+p` is bound
#: to `command.palette.show` (`keybind.ts:57`), and the footer renders
#: `{paletteShortcut()} commands`. The `commands` label survives keymap
#: rebinding. Also present while working (the footer's right side does not
#: change between idle and working), so it guards that the footer has drawn
#: at all, not that the pane is idle.
FOOTER_MARKER = "ctrl+p commands"

#: Rendered as the header of the permission modal
#: (`routes/session/permission.tsx:389`, also `:402` as the title).
APPROVAL_MARKER = "Permission required"

#: Rendered in the question modal's footer (`routes/session/question.tsx:509`,
#: JSX `esc <span>dismiss</span>`). The full footer is
#: `⇆ tab   ↑↓ select   enter submit   esc dismiss`.
QUESTION_MARKER = "esc dismiss"

#: How far up from the bottom to look for the working footer. Measured from
#: `tests/fixtures/screens/opencode_working.txt`: the footer line is the last
#: non-blank line before the tmux status bar, with 5 non-blank lines of
#: composer chrome above it.
_SCREEN_TAIL_LINES = 5

#: A finish that ends a step but not the turn.
STEP_FINISH = "tool-calls"

#: Events read per poll. A session that ran while the daemon was down could
#: have thousands queued; reading them in one gulp would block the observer.
DRAIN_LIMIT = 500

#: Extra files beside the per-participant OpenCode config. The plugin is also
#: the capability marker: an older live process has neither file, so its source
#: keeps the pre-receipt discovery behaviour after a Theater upgrade/restart.
CORRELATION_PLUGIN_SUFFIX = ".opencode.mjs"
CORRELATION_RECEIPT_SUFFIX = ".opencode-session"
CORRELATION_READY_TIMEOUT = 30.0


def _plugin_path(config_path: Path) -> Path:
    return config_path.with_suffix(CORRELATION_PLUGIN_SUFFIX)


def _receipt_path(config_path: Path) -> Path:
    return config_path.with_suffix(CORRELATION_RECEIPT_SUFFIX)


def _correlation_plugin(participant_id: str, receipt_path: Path) -> str:
    """A process-local OpenCode hook that publishes its exact root session.

    OpenCode's database is machine-global, so cwd and creation time cannot say
    which of two concurrent processes created a row. The plugin runs inside one
    process and sees that process's ``session.created`` event. It writes a tiny
    receipt beside Theater's generated config; the observer remains the reader
    and the daemon remains the only writer of Theater's SQLite state.
    """
    participant = json.dumps(participant_id)
    receipt = json.dumps(str(receipt_path))
    return f"""import {{ rename, writeFile }} from "node:fs/promises"

const participantID = {participant}
const receipt = {receipt}

async function publish(body) {{
  const pending = `${{receipt}}.${{process.pid}}.tmp`
  await writeFile(pending, JSON.stringify(body) + "\\n", "utf8")
  await rename(pending, receipt)
}}

export const TheaterSessionReceipt = async () => {{
  try {{
    await publish({{ participant_id: participantID, ready: true }})
  }} catch (error) {{
    console.error("theater session receipt failed to initialize", error)
  }}
  return {{
    event: async ({{ event }}) => {{
      if (event.type !== "session.created" || event.properties.info.parentID) return
      try {{
        await publish({{
          participant_id: participantID,
          session_id: event.properties.info.id,
        }})
      }} catch (error) {{
        console.error("theater session receipt failed to publish", error)
      }}
    }},
  }}
}}
"""


def _in_screen_tail(capture: str, marker: str) -> bool:
    """Whether any of the last few non-blank lines contains *marker* AND
    ``FOOTER_MARKER``.

    The working footer is chrome the CLI draws at the bottom of the pane, so
    searching the whole pane buys nothing — and matching the whole pane lets
    agent output (ordinary prose) impersonate chrome. An agent working on THIS
    repo will print the literal string ``esc interrupt`` in its own output.

    Scoping to the tail window is necessary but not sufficient on its own: the
    tail also contains the agent's closing lines. The co-occurrence guard is
    the second discriminator: the working footer renders the working marker
    and ``ctrl+p commands`` on the *same* line (the Prompt component's footer
    is a flexbox row with ``justifyContent="space-between"``, see
    ``component/prompt/index.tsx:1513``). Prose containing ``esc interrupt``
    does not also contain ``ctrl+p commands`` on the same line. Neither the
    tail window nor the co-occurrence test alone is enough; both are required.
    """
    lines = [line for line in capture.splitlines() if line.strip()]
    return any(marker in line and FOOTER_MARKER in line for line in lines[-_SCREEN_TAIL_LINES:])


def data_dir() -> Path:
    """Where opencode keeps its state. `$XDG_DATA_HOME` wins if it is set."""
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "opencode"


def _seconds(ms) -> float | None:
    """Milliseconds to a unix epoch float. None for anything else."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    return ms / 1000.0


def _loads(raw) -> dict:
    """A JSON column as a dict. Empty for anything that is not one.

    Rows are read from under a live writer, so a value that does not parse is
    an expected condition rather than a corruption to report.
    """
    if not isinstance(raw, (str, bytes)):
        return {}
    try:
        found = json.loads(raw)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _table(value) -> dict:
    """A nested object inside an already-parsed row. Empty for anything else.

    Written as a function rather than inline so the value is tested once:
    `x.get(k) if isinstance(x.get(k), dict) else {}` reads the key twice and
    leaves the result typed as the union it was before the test.
    """
    return value if isinstance(value, dict) else {}


def _tool_output(state: dict) -> str:
    """What a finished tool call produced, or what went wrong."""
    output = state.get("output")
    if isinstance(output, str):
        return output
    error = state.get("error")
    if isinstance(error, str):
        return error
    if error is not None:
        return json.dumps(error, default=str)
    return "" if output is None else json.dumps(output, default=str)


#: Tools whose `state.input.filePath` is a file they write to.
_WRITE_TOOLS = frozenset({"write", "edit"})

#: Tools whose `state.input.filePath` is a file they read.
_READ_TOOLS = frozenset({"read"})


def _relativise(path: str, cwd: str | None) -> str | None:
    """Make a path repo-relative, or None if it cannot be done safely.

    opencode's `filePath` may be absolute or relative to the session's
    working directory (write.ts:41-43, edit.ts:80-82 resolve it against
    `instance.directory`). We relativise against `cwd`, which is the
    directory the source was constructed with — the same value the daemon
    uses to locate the session row. A path already relative is returned
    unchanged, on the assumption that it is already repo-relative; this is
    correct for opencode, which resolves relative paths against the session
    directory at execution time and stores them as given.

    Both sides are resolved before comparison, because macOS aliases
    ``/tmp`` as ``/private/tmp`` and a mismatch there would drop a path that
    is genuinely inside the repo. The session directory in the database is
    also stored resolved (see ``_locate``), so this is consistent with how
    the source already treats paths.

    None is returned when the path is outside the repo root, because an
    absolute path that does not start with `cwd` is either a temp file or a
    path into another project — both of which would pollute the index with
    false entries. Better to record nothing than to record a wrong path.
    """
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        return path
    if cwd is None:
        # Returning the absolute path would leak a home directory into the
        # index; None drops the path, which is the safer failure mode.
        return None
    try:
        rel = p.resolve().relative_to(Path(cwd).resolve())
    except (ValueError, OSError):
        return None
    return str(rel)


def _paths_from_tool(name: str, state: dict, cwd: str | None) -> tuple[EventPath, ...]:
    """Extract file paths from a tool call's structured input.

    Only `state.input` is read — the decoded JSON arguments the LLM passed.
    Paths are never parsed out of shell command strings or patch text; the
    contract is that a wrong path is worse than a missing one, and parsing
    prose or commands is exactly where wrong paths come from.

    `glob` and `grep` take a `path` field, but it is a directory to search
    within, not a file. Per the design, a search over a directory yields no
    paths. `apply_patch` embeds paths inside a `patchText` string, which is
    the same class of unstructured input we decline to parse. `bash`/`shell`
    has no file path field in its structured input at all.
    """
    if not name or name in ("bash", "shell", "apply_patch", "glob", "grep", "webfetch"):
        return ()
    input_data = state.get("input")
    if not isinstance(input_data, dict):
        return ()
    raw = input_data.get("filePath")
    if not isinstance(raw, str):
        return ()
    rel = _relativise(raw, cwd)
    if rel is None:
        return ()
    mode: Literal["read", "write"] = "write" if name in _WRITE_TOOLS else "read"
    return (EventPath(path=rel, mode=mode),)


class OpenCodeHarness(Harness):
    name = "opencode"
    binary = "opencode"
    #: An open lozenge, distinguishable from the three already taken — `▤`
    #: (vibe), `✻` (claude), `◉` (codex) — at one column, in any font with
    #: Geometric Shapes.
    icon = "\u25c7"
    #: A spelling that does not normalize is observed as nothing at all, so
    #: these are not cosmetic.
    aliases = ("open-code", "open_code", "OpenCode", "opencode-ai")
    #: `-s` routes to the session view (app.tsx:492-496) and `--prompt` is only
    #: read on the home screen (home.tsx:53-54, 64-67), so a prompt passed
    #: alongside `-s` is silently dropped and the task vanishes with no error.
    #: This is why the capability is a class attribute and not signature
    #: introspection: a signature can express "accepts a resume flag", but it
    #: cannot express "accepts it and drops your prompt".
    resume_takes_prompt: bool = False

    def __init__(self, db: Path | None = None, correlation_dir: Path | None = None):
        #: The observer's business alone; nothing about launching depends on it.
        self.observer = OpenCodeObserver(db=db, correlation_dir=correlation_dir)

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
            raise BadRequest(f"approval must be one of {', '.join(APPROVALS)}, got {approval!r}")
        config = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                SERVER_NAME: {
                    "type": "local",
                    "enabled": True,
                    "command": [theater_binary(), "mcp", "--id", participant_id],
                }
            },
        }
        plugin_path = _plugin_path(config_path)
        receipt_path = _receipt_path(config_path)
        # OpenCode keeps all processes' sessions in one machine-global SQLite
        # database. This per-process hook supplies the correlation fact the
        # database itself does not contain. Plugin specs from separate config
        # sources are merged/deduplicated by OpenCode, so this does not replace
        # plugins in the user's own config.
        config["plugin"] = [plugin_path.resolve().as_uri()]
        argv = ["opencode"]
        if model:
            # opencode wants `provider/model`, not a bare model name. Passed
            # through as given: which providers a user has configured is not
            # something this adapter can know.
            argv += ["--model", model]
        if approval == "yolo":
            argv.append("--auto")
        if resume is not None:
            # `--prompt` is omitted here on purpose: `-s` routes to the session
            # view (app.tsx:492) and `--prompt` is only read on the home screen
            # (home.tsx:53-54, 64-67), so passing both silently drops the task.
            argv += ["-s", resume]
        elif prompt:
            argv += ["--prompt", prompt]
        files = {
            config_path: json.dumps(config, indent=2),
            plugin_path: _correlation_plugin(participant_id, receipt_path),
        }
        # Resuming does not create a session, so there is no creation event for
        # the hook to observe. The requested id is already an exact receipt.
        if resume is not None:
            files[receipt_path] = (
                json.dumps({"participant_id": participant_id, "session_id": resume}) + "\n"
            )
        return LaunchPlan(
            argv=argv,
            env={"OPENCODE_CONFIG": str(config_path)},
            files=files,
            session_id=resume,
        )

    def discover_models(self) -> list[str]:
        """`opencode models`, which prints one `provider/model` per line.

        The only shipped harness with a real listing command, and it prints
        exactly the spelling `--model` wants, so the output needs no
        translation. It reflects the providers this user has authenticated,
        which is why it is worth asking rather than hardcoding — and equally
        why the answer belongs in the user's config file and not in a cache
        Theater manages.
        """
        try:
            out = subprocess.check_output(
                [self.binary, "models"],
                text=True,
                timeout=MODELS_TIMEOUT,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise NotImplementedError(f"{self.binary} is not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise NotImplementedError(
                f"`{self.binary} models` did not answer within {MODELS_TIMEOUT}s"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise NotImplementedError(f"`{self.binary} models` failed: {exc}") from exc
        # An empty result is what no authenticated provider looks like.
        return [line.strip() for line in out.splitlines() if line.strip()]


class OpenCodeObserver(HarnessObserver):
    """Read one session's rows out of the shared opencode database.

    The adapter that motivated splitting observation off `Harness` in v1.6.
    While the two were one interface this class's four transcript methods —
    `find_transcript`, `session_id`, `parse`, `native_children` — existed only
    to return nothing, because none of those questions has an answer when the
    output is a database rather than a file. Subclassing `HarnessObserver`
    directly instead of `TranscriptObserver` deletes all four.

    `has_transcript` stays True regardless: it means "can be observed by
    reading", not "writes a file", and this adapter reads better than the
    file-backed ones do.

    Known gap, and the reason `native_children` is left at its inherited empty
    default rather than implemented: opencode does have sub-agents and they are
    discoverable — `session.parent_id` points at the parent — but the method is
    keyed by transcript path and there is no path. Surfacing them needs a
    lineage hook on `Source`.
    """

    def __init__(self, db: Path | None = None, correlation_dir: Path | None = None):
        #: Injectable so tests never touch the real database.
        self.db = db or data_dir() / DB_NAME
        #: Where launch plans put their plugin marker and session receipt.
        self.correlation_dir = correlation_dir

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Source:
        return OpenCodeSource(self.db, cwd=cwd, session_id=session_id, after=after)

    def open_source_for(
        self,
        *,
        participant_id: str,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
    ) -> Source:
        """Use an exact process receipt when this participant launched with one.

        The generated plugin file is the capability marker. Participants that
        were already running when Theater gained receipts have no marker and
        retain the cwd/time fallback; newly launched processes fail closed and
        wait for their own receipt instead of claiming a sibling's session.
        """
        config_path = (
            self.correlation_dir / f"{participant_id}.json"
            if self.correlation_dir is not None
            else paths.mcp_config_path(participant_id)
        )
        plugin_path = _plugin_path(config_path)
        receipt_path = _receipt_path(config_path) if plugin_path.exists() else None
        return OpenCodeSource(
            self.db,
            cwd=cwd,
            session_id=session_id,
            after=after,
            participant_id=participant_id,
            receipt=receipt_path,
            session_provenance=session_provenance,
            known_location=known_location,
        )

    def is_idle_screen(self, capture: str) -> bool:
        """Decided by the absence of the working markers from the footer.

        Weaker than the other adapters, which match a prompt they can see. The
        composer placeholder (`Ask anything...`) disappears once a conversation
        exists, so there is no positive marker to match after the first turn —
        only the working markers, and their absence. The footer hint guards the
        case that costs the most: a pane that has not drawn yet is blank, and a
        blank capture must not read as a prompt.

        Uses the same tail-scoped co-occurrence test as ``screen_reading`` so
        that agent prose containing ``esc interrupt`` on an idle pane does not
        suppress idleness.
        """
        if any(_in_screen_tail(capture, m) for m in WORKING_MARKERS):
            return False
        return FOOTER_MARKER in capture

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify the rendered screen as `working`, `approval`, or `prompt`.

        The arms and their ordering are load-bearing:

        Working first, because the working footer (`esc interrupt` /
        `esc again to interrupt`) is tail-scoped with a co-occurrence guard
        (see ``_in_screen_tail``). Both spellings are matched, and neither is
        a substring of `· interrupted` (rendered in the message log after an
        abort, `routes/session/index.tsx:1569`), so an idle pane after an abort
        does not read WORKING.

        Modal arms (approval, question) are gated on the *absence* of all
        prompt-component chrome. When a permission or question modal is up,
        `routes/session/index.tsx:241` defines
        ``visible = !session().parentID && permissions().length === 0 &&
        questions().length === 0`` and the Prompt component only renders inside
        ``<Show when={visible()}>`` (index.tsx:1313). So when a modal is up,
        neither `esc interrupt` (WORKING_MARKERS) nor `ctrl+p commands`
        (FOOTER_MARKER) is on screen. On a genuine modal the prompt chrome is
        absent; on an agent merely echoing the words `Permission required` or
        `esc dismiss`, the composer footer or spinner is still there. This gate
        prevents agent output from impersonating a modal — an agent working on
        THIS repo that prints the fixture text would otherwise classify itself
        APPROVAL and become unreachable through the send gate.

        Both modals classify as APPROVAL (HIGH), not as distinct screen kinds.
        A question screen is functionally an approval: the agent is blocked
        and Enter commits a choice. This keeps the reducer and send gate
        untouched — the send gate blocks APPROVAL at HIGH confidence
        (``theater/daemon/methods.py:527-537``).
        """
        if any(_in_screen_tail(capture, m) for m in WORKING_MARKERS):
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        prompt_chrome = FOOTER_MARKER in capture or any(m in capture for m in WORKING_MARKERS)
        if not prompt_chrome and (APPROVAL_MARKER in capture or QUESTION_MARKER in capture):
            return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
        if self.is_idle_screen(capture):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)

    def transcript_candidates(
        self,
        *,
        cwd: str | None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        if not self.db.exists() or not cwd:
            return []
        want = str(Path(cwd).resolve())
        try:
            st = self.db.stat()
        except OSError:
            st = None
        rows: list[TranscriptCandidate] = []
        try:
            conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            try:
                sql = (
                    "SELECT id, directory, time_created FROM session "
                    "WHERE parent_id IS NULL ORDER BY time_created DESC"
                )
                for sid, directory, created in conn.execute(sql):
                    reason = None
                    before_floor = (
                        after is not None
                        and isinstance(created, (int, float))
                        and created < after * 1000
                    )
                    if before_floor:
                        reason = "created before participant floor"
                    elif directory != want:
                        reason = "cwd mismatch"
                    rows.append(
                        TranscriptCandidate(
                            location=f"opencode://{sid}",
                            session_id=str(sid),
                            mtime=st.st_mtime if st else None,
                            size=st.st_size if st else None,
                            rejection_reason=reason,
                            domain=f"opencode://{self.db.resolve()}",
                        )
                    )
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        return rows

    def admit_operator_candidate(
        self,
        *,
        cwd: str | None,
        candidate: str,
        domain: str | None = None,
        after: float | None = None,
    ) -> TranscriptCandidate:
        if not self.db.exists():
            raise ValueError("OpenCode database does not exist")
        if domain is not None and domain != f"opencode://{self.db.resolve()}":
            raise ValueError("candidate session is outside this harness transcript domain")
        sid = candidate.removeprefix("opencode://")
        if not sid:
            raise ValueError("unextractable session id")
        want = str(Path(cwd).resolve()) if cwd else None
        try:
            st = self.db.stat()
            conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT id, directory, time_created FROM session "
                    "WHERE id = ? AND parent_id IS NULL",
                    (sid,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise ValueError("OpenCode database is not readable") from exc
        if row is None:
            raise ValueError("harness shape mismatch")
        if want is not None and row[1] != want:
            raise ValueError("cwd mismatch")
        if after is not None and isinstance(row[2], (int, float)) and row[2] < after * 1000:
            raise ValueError("created before participant floor")
        return TranscriptCandidate(
            location=f"opencode://{row[0]}",
            session_id=str(row[0]),
            mtime=st.st_mtime,
            size=st.st_size,
            domain=f"opencode://{self.db.resolve()}",
        )


class OpenCodeSource(Source):
    """Tail one session's rows in the shared opencode database.

    Holds the connection open for the life of the watcher, and a little state
    the event stream requires: which message each part belongs to, the text
    buffered for a message that has not finished, and how far each tool call
    has got. That state is per session and is dropped on every attach.

    Every query runs under `asyncio.to_thread`, and the connection is opened
    with `check_same_thread=False` because that pool hands out whichever thread
    is free. Concurrent use is not a risk: the observer runs one task per
    participant and awaits each read before the next.
    """

    def __init__(
        self,
        db: Path,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        participant_id: str | None = None,
        receipt: Path | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
    ) -> None:
        self._db = db
        self._cwd = cwd
        #: Set after an accepted attach, so a later re-open can use the sharper key.
        self._session_id = session_id
        self._session_provenance = normalize_provenance(session_provenance)
        self._session_exact = self._session_provenance is TranscriptProvenance.EXACT
        self._known_location = known_location
        self._known_location_provenance = (
            self._session_provenance
            if self._known_location is not None
            else TranscriptProvenance.HEURISTIC
        )
        self._after = after
        self._participant_id = participant_id
        self._receipt = receipt
        self._receipt_started = time.monotonic()
        self._conn: sqlite3.Connection | None = None
        self._session: str | None = None
        self._cursor = -1
        self._pending: tuple[str, int] | None = None
        self._located_exact = False
        self._located_receipt_sid: str | None = None
        #: message id -> role. Filled from the event stream and, for a message
        #: whose creation we skipped at attach, from the `message` table.
        self._roles: dict[str, str] = {}
        #: message id -> {part id: text}. Insertion-ordered, so joining the
        #: values reassembles a multi-part reply in the order it was written.
        self._text: dict[str, dict[str, str]] = {}
        #: call id -> last status seen, so one tool call yields one TOOL_CALL
        #: and one TOOL_RESULT however many updates it takes.
        self._tools: dict[str, str] = {}
        #: message id -> the time of the last part it produced. The finish
        #: event carries `time.completed` only on its *second* firing, which
        #: is deduped away, so without this a reply would be stamped with when
        #: the model started rather than when it stopped.
        self._stamp: dict[str, float] = {}
        self._finished: set[str] = set()
        self._said: set[str] = set()

    # ---- Source ---------------------------------------------------------

    async def read(self) -> Batch:
        return await asyncio.to_thread(self._read)

    async def refresh(self) -> Batch:
        return await asyncio.to_thread(self._refresh)

    def commit_attachment(self) -> None:
        """Adopt the session after the observer accepts its binding."""
        if self._pending is None:
            raise RuntimeError("no opencode attachment is pending")
        session, cursor = self._pending
        provenance = normalize_provenance(self._attachment_provenance(session))
        self._session, self._cursor = session, cursor
        self._session_id = self._session
        self._known_location = f"opencode://{self._session}"
        self._session_provenance = provenance
        self._session_exact = provenance is TranscriptProvenance.EXACT
        self._known_location_provenance = provenance
        self._pending = None
        self._roles.clear()
        self._text.clear()
        self._tools.clear()
        self._stamp.clear()
        self._finished.clear()
        self._said.clear()

    def discard_attachment(self) -> None:
        """Reject a session candidate without disturbing the accepted one."""
        if self._pending is None:
            raise RuntimeError("no opencode attachment is pending")
        self._pending = None

    def revoke_attachment(self) -> None:
        """Forget a heuristic session superseded by a process-local receipt."""
        self._pending = None
        self._session = None
        self._session_id = None
        self._session_provenance = TranscriptProvenance.HEURISTIC
        self._session_exact = False
        self._known_location = None
        self._known_location_provenance = TranscriptProvenance.HEURISTIC
        self._located_receipt_sid = None
        self._cursor = -1
        self._roles.clear()
        self._text.clear()
        self._tools.clear()
        self._stamp.clear()
        self._finished.clear()
        self._said.clear()

    async def history(self, *, last_n: int) -> History:
        return await asyncio.to_thread(self._history, last_n)

    async def aclose(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                logger.debug("closing the opencode database failed", exc_info=True)

    # ---- synchronous bodies ---------------------------------------------

    def _read(self) -> Batch:
        self._require_decision()
        conn = self._open()
        if conn is None:
            return Batch(waiting=True)
        try:
            if self._session is None:
                found = self._locate(conn, pinned=True)
                if found:
                    return self._attach(conn, found)
                return self._correlation_problem(conn) or Batch(waiting=True)
            return self._drain(conn)
        except sqlite3.Error:
            # Opened read-only under a live writer; a lock or transient error
            # is a missed poll, not a dead watcher.
            logger.debug("reading the opencode database failed", exc_info=True)
            return Batch()

    def _refresh(self) -> Batch:
        """Propose the newest session for this directory if there is one.

        The pinned session id is ignored, for the reason `TranscriptSource`
        ignores it: a human can start a fresh session inside the same pane, and
        the id we stored names one that will never grow again.
        """
        self._require_decision()
        if self._receipt is None:
            # A machine-global cwd scan can only find "newer", not "mine".
            # Legacy sources retain their accepted session instead.
            return Batch()
        conn = self._open()
        if conn is None:
            return Batch()
        try:
            found = self._locate(conn, pinned=False)
            if found is None or found == self._session:
                return Batch()
            logger.info("opencode session changed: %s -> %s", self._session, found)
            return self._attach(conn, found)
        except sqlite3.Error:
            logger.debug("relocating the opencode session failed", exc_info=True)
            return Batch()

    def _history(self, last_n: int) -> History:
        """Rebuild the conversation from `message` and `part`.

        Those tables hold current state, so this never sees the empty-then-full
        intermediates the event stream carries. Read independently of the poll
        cursor: the caller is usually a short-lived source of its own.
        """
        conn = self._open()
        if conn is None:
            return History()
        pinned_sid = None
        if self._known_location and self._known_location.startswith("opencode://"):
            pinned_sid = self._known_location.removeprefix("opencode://") or None
        pinned = self._session is None and pinned_sid is not None
        try:
            sid = self._session or pinned_sid or self._locate(conn, pinned=True)
            if sid is None:
                problem = self._correlation_problem(conn)
                return (
                    History(error_code=problem.error_code, error=problem.error)
                    if problem is not None
                    else History()
                )
            parts: dict[str, list[dict]] = {}
            rows = conn.execute(
                "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created, id",
                (sid,),
            )
            for mid, raw in rows:
                parts.setdefault(mid, []).append(_loads(raw))
            events: list[Event] = []
            rows = conn.execute(
                "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created, id",
                (sid,),
            )
            for mid, raw in rows:
                events.extend(self._replay(_loads(raw), parts.get(mid, [])))
        except sqlite3.Error:
            logger.debug("reading opencode history failed", exc_info=True)
            return History()
        if last_n > 0:
            events = events[-last_n:]
        # Stored rows carry no sequence number, so position stands in for one.
        events = [replace(e, raw_index=i) for i, e in enumerate(events)]
        return History(
            location=f"opencode://{sid}",
            events=events,
            correlation=self._attachment_provenance(sid),
            pinned=pinned,
        )

    def _replay(self, info: dict, parts: list[dict]) -> list[Event]:
        """One stored message, as events. Text unclipped: this is history."""
        time = _table(info.get("time"))
        ts = _seconds(time.get("completed")) or _seconds(time.get("created"))
        text = "".join(p.get("text") or "" for p in parts if p.get("type") == "text")
        if info.get("role") != "assistant":
            return (
                [Event(kind=EventKind.USER, text=whole(text), raw_text=text, ts=ts)] if text else []
            )

        out: list[Event] = []
        for part in parts:
            if part.get("type") != "tool":
                continue
            state = _table(part.get("state"))
            name = part.get("tool")
            paths = _paths_from_tool(name or "", state, self._cwd)
            out.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=name,
                    ts=ts,
                    paths=paths,
                )
            )
            if state.get("status") in ("completed", "error"):
                raw = _tool_output(state)
                out.append(
                    Event(
                        kind=EventKind.TOOL_RESULT,
                        text=whole(raw),
                        raw_text=raw,
                        tool_name=name,
                        ts=ts,
                    )
                )
        finish = info.get("finish")
        turn_end = bool(finish) and finish != STEP_FINISH
        if text or turn_end:
            out.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    text=whole(text),
                    raw_text=text,
                    ts=ts,
                    turn_end=turn_end,
                    turn_id=info.get("id") or None,
                )
            )
        return out

    # ---- internals ------------------------------------------------------

    def _open(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        if not self._db.exists():
            return None
        try:
            self._conn = sqlite3.connect(
                f"file:{self._db}?mode=ro", uri=True, check_same_thread=False
            )
        except sqlite3.Error:
            logger.debug("opening %s failed", self._db, exc_info=True)
            return None
        return self._conn

    def _locate(self, conn: sqlite3.Connection, *, pinned: bool) -> str | None:
        # A receipt is an exact process-local claim and therefore outranks a
        # stored session id that may have come from the old ambiguous fallback.
        # Do not fall through while waiting: that would reintroduce the race
        # during the few milliseconds between session creation and receipt.
        if self._receipt is not None:
            sid = self._read_receipt()
            if sid is None:
                self._located_exact = False
                self._located_receipt_sid = None
                return None
            row = conn.execute(
                "SELECT id FROM session WHERE id = ? AND parent_id IS NULL",
                (sid,),
            ).fetchone()
            self._located_exact = row is not None
            self._located_receipt_sid = row[0] if row is not None else None
            return row[0] if row is not None else None
        if pinned and self._session_id:
            row = conn.execute(
                "SELECT id FROM session WHERE id = ?", (self._session_id,)
            ).fetchone()
            if row is not None:
                self._located_exact = self._session_exact
                self._located_receipt_sid = None
                return row[0]
        if not self._cwd:
            self._located_exact = False
            self._located_receipt_sid = None
            return None
        want = str(Path(self._cwd).resolve())
        sql = "SELECT id FROM session WHERE directory = ? AND parent_id IS NULL"
        args: list = [want]
        if self._after is not None:
            sql += " AND time_created >= ?"
            args.append(int(self._after * 1000))
        self._located_exact = False
        self._located_receipt_sid = None
        # Legacy discovery is a candidate, not an ownership claim. The reducer
        # rejects it when a same-cwd competitor makes it ambiguous.
        count_sql = sql.replace("SELECT id", "SELECT COUNT(*)")
        count = conn.execute(count_sql, args).fetchone()
        if count is not None and count[0] > 1:
            logger.warning(
                "opencode _locate: %d sessions match cwd %s; "
                "returning a heuristic candidate for the reducer to validate",
                count[0],
                self._cwd,
            )
        sql += " ORDER BY time_created DESC LIMIT 1"
        row = conn.execute(sql, args).fetchone()
        return row[0] if row is not None else None

    def _read_receipt(self) -> str | None:
        """Read and validate the process-local participant/session receipt."""
        if self._receipt is None or self._participant_id is None:
            return None
        try:
            found = json.loads(self._receipt.read_text())
        except (FileNotFoundError, OSError, ValueError):
            return None
        if not isinstance(found, dict) or found.get("participant_id") != self._participant_id:
            return None
        sid = found.get("session_id")
        return sid if isinstance(sid, str) and sid else None

    def _correlation_problem(self, conn: sqlite3.Connection) -> Batch | None:
        """Make a missing exact channel visible after a bounded startup."""
        if self._receipt is None or self._participant_id is None:
            return None
        if time.monotonic() - self._receipt_started < CORRELATION_READY_TIMEOUT:
            return None
        try:
            found = json.loads(self._receipt.read_text())
        except (FileNotFoundError, OSError, ValueError):
            found = None
        if not isinstance(found, dict) or found.get("participant_id") != self._participant_id:
            return Batch(
                waiting=True,
                error_code="transcript_correlation_failed",
                error="OpenCode's Theater correlation plugin did not initialize",
            )
        # A ready marker may legitimately wait forever in a promptless pane.
        # Once a matching root session exists, the same process should already
        # have published its exact id through session.created.
        if not self._cwd:
            return None
        sql = "SELECT 1 FROM session WHERE directory = ? AND parent_id IS NULL"
        args: list = [str(Path(self._cwd).resolve())]
        if self._after is not None:
            sql += " AND time_created >= ?"
            args.append(int(self._after * 1000))
        if conn.execute(sql + " LIMIT 1", args).fetchone() is None:
            return None
        return Batch(
            waiting=True,
            error_code="transcript_correlation_failed",
            error="OpenCode created a session but its exact Theater receipt is missing",
        )

    def _attach(self, conn: sqlite3.Connection, sid: str) -> Batch:
        """Stage a cursor at the end of a session's events.

        History is skipped rather than replayed, as everywhere else. Status is
        reported explicitly here — it is the one moment the source knows
        something the event stream cannot say, since a session that finished
        before we found it will produce no further events to infer from.
        """
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1), COUNT(*) FROM event WHERE aggregate_id = ?",
            (sid,),
        ).fetchone()
        status = self._status(conn, sid)
        self._pending = (sid, row[0])
        return Batch(
            attached=Attachment(
                location=f"opencode://{sid}",
                session_id=sid,
                skipped=row[1],
                correlation=self._attachment_provenance(sid),
            ),
            status=status,
        )

    def _attachment_provenance(self, sid: str) -> str:
        if self._known_location == f"opencode://{sid}" and is_trusted_provenance(
            self._known_location_provenance
        ):
            return str(self._known_location_provenance)
        if self._located_receipt_sid == sid:
            return str(TranscriptProvenance.EXACT)
        if self._session_exact and self._session_id == sid:
            return str(TranscriptProvenance.EXACT)
        return str(TranscriptProvenance.HEURISTIC)

    def _require_decision(self) -> None:
        if self._pending is not None:
            raise RuntimeError("attachment must be committed or discarded before reading again")

    def _status(self, conn: sqlite3.Connection, sid: str) -> Status:
        """Idle or working, from the newest message.

        A session with no messages is idle: opencode writes the session row
        when the TUI boots, and the prompt's message follows tens of
        milliseconds later. Landing inside that window costs one poll of
        wrongness; calling it WORKING would instead leave a session launched
        with no prompt looking busy until something else moved it.
        """
        row = conn.execute(
            "SELECT data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if row is None:
            return Status.IDLE
        info = _loads(row[0])
        if info.get("role") != "assistant":
            return Status.WORKING
        time = _table(info.get("time"))
        finish = info.get("finish")
        if finish and finish != STEP_FINISH and time.get("completed"):
            return Status.IDLE
        return Status.WORKING

    def _drain(self, conn: sqlite3.Connection) -> Batch:
        rows = conn.execute(
            "SELECT seq, type, data FROM event WHERE aggregate_id = ? AND seq > ? "
            "ORDER BY seq LIMIT ?",
            (self._session, self._cursor, DRAIN_LIMIT),
        ).fetchall()
        if not rows:
            return Batch()
        events: list[Event] = []
        for seq, kind, raw in rows:
            self._cursor = seq
            events.extend(self._translate(conn, kind, _loads(raw), seq))
        # Rows consumed is progress even when none produced an event:
        # session.updated fires throughout a turn, and reading it as silence
        # would let the rescue timer fire mid-turn.
        return Batch(events=events, progressed=True)

    def _translate(
        self, conn: sqlite3.Connection, kind: str, payload: dict, seq: int
    ) -> list[Event]:
        if kind == "message.part.updated.1":
            return self._on_part(conn, payload, seq)
        if kind == "message.updated.1":
            return self._on_message(payload, seq)
        # session.created / session.updated: progress, not conversation.
        return []

    def _on_part(self, conn: sqlite3.Connection, payload: dict, seq: int) -> list[Event]:
        part = payload.get("part")
        if not isinstance(part, dict):
            return []
        ts = _seconds(payload.get("time"))
        mid = part.get("messageID")
        if ts is not None and isinstance(mid, str):
            self._stamp[mid] = ts
        ptype = part.get("type")
        if ptype == "text":
            return self._on_text(conn, part, ts, seq)
        if ptype == "tool":
            return self._on_tool(part, ts, seq)
        return []

    def _on_text(
        self, conn: sqlite3.Connection, part: dict, ts: float | None, seq: int
    ) -> list[Event]:
        mid = part.get("messageID") or ""
        text = part.get("text") or ""
        if self._role(conn, mid) != "assistant":
            pid = part.get("id") or ""
            if not text or pid in self._said:
                return []
            self._said.add(pid)
            return [
                Event(
                    kind=EventKind.USER,
                    text=clip(text),
                    raw_text=text,
                    ts=ts,
                    raw_index=seq,
                )
            ]
        # Replaced, not appended: each update carries the whole part.
        self._text.setdefault(mid, {})[part.get("id") or ""] = text
        return []

    def _on_tool(self, part: dict, ts: float | None, seq: int) -> list[Event]:
        state = _table(part.get("state"))
        status = state.get("status")
        if not status or status == "pending":
            # Pending may never run at all.
            return []
        call = part.get("callID") or part.get("id") or ""
        name = part.get("tool")
        seen = self._tools.get(call)
        out: list[Event] = []
        if seen is None:
            # `running` is the first status that carries `state.input`, so
            # paths are available here, not before. Attached to the TOOL_CALL
            # because the call references the file; the result is its outcome.
            paths = _paths_from_tool(name or "", state, self._cwd)
            out.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=name,
                    ts=ts,
                    raw_index=seq,
                    paths=paths,
                )
            )
        done = ("completed", "error")
        if status in done and seen not in done:
            raw = _tool_output(state)
            out.append(
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=clip(raw),
                    raw_text=raw,
                    tool_name=name,
                    ts=ts,
                    raw_index=seq,
                )
            )
        self._tools[call] = status
        return out

    def _on_message(self, payload: dict, seq: int) -> list[Event]:
        info = payload.get("info")
        if not isinstance(info, dict):
            return []
        mid = info.get("id") or ""
        role = info.get("role")
        if isinstance(role, str):
            self._roles[mid] = role
        if role != "assistant":
            return []
        finish = info.get("finish")
        if not finish or mid in self._finished:
            return []
        self._finished.add(mid)
        time = _table(info.get("time"))
        ts = (
            _seconds(time.get("completed"))
            or self._stamp.pop(mid, None)
            or _seconds(time.get("created"))
        )
        text = "".join(self._text.pop(mid, {}).values())
        turn_end = finish != STEP_FINISH
        if not text and not turn_end:
            # Tool events already reported it; an empty line would be noise.
            return []
        return [
            Event(
                kind=EventKind.ASSISTANT,
                text=clip(text),
                raw_text=text,
                ts=ts,
                turn_end=turn_end,
                # The assistant message id. Every part of the reply references
                # it as `message_id`, so it names the turn as well as the
                # message — opencode ends a turn by finishing a message.
                turn_id=mid or None,
                raw_index=seq,
            )
        ]

    def _role(self, conn: sqlite3.Connection, mid: str) -> str | None:
        """The role of a message, from the stream or from the table.

        The fallback exists for the message whose creation event was skipped at
        attach: its parts keep arriving, and without a role they would be
        attributed to whichever branch guessed.
        """
        role = self._roles.get(mid)
        if role is not None:
            return role
        row = conn.execute("SELECT data FROM message WHERE id = ?", (mid,)).fetchone()
        found = _loads(row[0]).get("role") if row is not None else None
        if isinstance(found, str):
            self._roles[mid] = found
            return found
        return None


#: What the loader looks for. An instance, not the class (see
#: docs/harness-plugins.md). Shipped adapters meet the same contract as
#: anything in $THEATER_HOME/harnesses.
HARNESS = OpenCodeHarness()
