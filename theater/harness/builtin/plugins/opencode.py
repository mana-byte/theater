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
import base64
import binascii
import hashlib
import json
import logging
import math
import os
import sqlite3
import subprocess
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from theater import paths
from theater.harness.base import (
    APPROVALS,
    SERVER_NAME,
    Event,
    EventKind,
    EventPath,
    Harness,
    LaunchPlan,
    ResumeLaunchOverlay,
    TokenUsage,
    clip,
    theater_binary,
    whole,
)
from theater.harness.contracts.source import HistoryPage
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.observation import (
    HarnessObserver,
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.source import (
    Attachment,
    Batch,
    History,
    Source,
    TranscriptCandidate,
)
from theater.models import BadRequest, Status

if TYPE_CHECKING:
    from theater.models import Participant
from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_PAGE_RECORD_LIMIT,
)
from theater.provenance import (
    TranscriptProvenance,
    is_trusted_provenance,
    normalize_provenance,
)
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import (
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)
from theater.trajectory.records import Timing, TrajectoryUsage
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
)

logger = logging.getLogger("theater.harness.opencode")

#: Reported by `opencode debug paths`. The `-stable` suffix is the release channel.
DB_NAME = "opencode-stable.db"

#: Seconds `opencode models` gets before `theater models --discover` gives up.
MODELS_TIMEOUT = 20

#: Working footer spellings. Do NOT shorten to interrupt: interrupted contains it. Guarded in tail.
WORKING_MARKERS = ("esc interrupt", "again to interrupt")

#: The idle footer's right-hand hint. Also present while working — guards footer drew, not idle.
FOOTER_MARKER = "ctrl+p commands"

#: Rendered as the header of the permission modal.
APPROVAL_MARKER = "Permission required"

#: Rendered in the question modal's footer.
QUESTION_MARKER = "esc dismiss"

#: How far up from the bottom to look for the working footer.
_SCREEN_TAIL_LINES = 5

#: A finish that ends a step but not the turn.
STEP_FINISH = "tool-calls"

#: Events read per poll; reading thousands queued in one gulp would block the observer.
DRAIN_LIMIT = 500

#: Extra files beside the per-participant config. The plugin is also the receipt capability marker.
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


def _opencode_usage(info: dict) -> TokenUsage | None:
    """Extract usage from an OpenCode assistant message."""
    tokens = info.get("tokens")
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache") or {}
    cost = info.get("cost")
    # OpenCode uses zero when it has no per-turn price; zero falls through to model pricing.
    cost = float(cost) if isinstance(cost, (int, float)) and cost > 0 else None
    provider = info.get("providerID")
    model_id = info.get("modelID")
    if isinstance(provider, str) and isinstance(model_id, str) and provider and model_id:
        model = f"{provider}/{model_id}"
    elif isinstance(model_id, str) and model_id:
        model = model_id
    else:
        model = None
    native_id = info.get("id")
    usage_key = f"opencode:{native_id}" if isinstance(native_id, str) and native_id else None
    return TokenUsage(
        model=model,
        input_tokens=int(tokens.get("input") or 0),
        output_tokens=int(tokens.get("output") or 0),
        cache_creation_input_tokens=int(cache.get("write") or 0),
        cache_read_input_tokens=int(cache.get("read") or 0),
        reasoning_output_tokens=int(tokens.get("reasoning") or 0),
        cost_usd=cost,
        idempotency_key=usage_key,
    )


def _opencode_model(info: dict) -> str | None:
    model_data = _table(info.get("model"))
    provider = info.get("providerID") or model_data.get("providerID")
    model_id = info.get("modelID") or model_data.get("modelID") or model_data.get("id")
    if isinstance(provider, str) and isinstance(model_id, str) and provider and model_id:
        return f"{provider}/{model_id}"
    if isinstance(model_id, str) and model_id:
        return model_id
    return None


def _table(value) -> dict:
    """A nested object inside an already-parsed row. Empty for anything else.

    Written as a function rather than inline so the value is tested once:
    `x.get(k) if isinstance(x.get(k), dict) else {}` reads the key twice and
    leaves the result typed as the union it was before the test.
    """
    return value if isinstance(value, dict) else {}


def _trajectory_identifier(value, prefix: str = "id") -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        return None
    if len(encoded) <= TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return value
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def _trajectory_text(value) -> str:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", errors="replace").decode("utf-8")
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, default=str, sort_keys=True)
        except (TypeError, ValueError):
            return ""
    return ""


def _trajectory_string(value) -> str:
    return value if isinstance(value, str) else ""


def _trajectory_lane(kind: TrajectoryKind) -> TrajectoryLane:
    if kind is TrajectoryKind.USER:
        return TrajectoryLane.INPUT
    if kind in (TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT):
        return TrajectoryLane.TOOLS
    return TrajectoryLane.MODEL


def _trajectory_detail(
    name: str, value, *, format: ContentFormat = ContentFormat.TEXT
) -> DetailField | None:
    if value is None:
        return None
    text = _trajectory_text(value)
    if not text and isinstance(value, (int, float, bool)):
        text = json.dumps(value)
    if not text:
        return None
    try:
        return DetailField.from_text(name, text, format=format)
    except ValueError:
        return None


def _trajectory_seconds(value) -> float | None:
    found = _seconds(value)
    return found if found is not None and math.isfinite(found) else None


def _trajectory_timing(start, end, fallback=None) -> Timing | None:
    start_value = _trajectory_seconds(start)
    end_value = _trajectory_seconds(end)
    if start_value is None and end_value is None and fallback is not None:
        start_value = _trajectory_seconds(fallback)
    if start_value is None and end_value is None:
        return None
    if end_value is not None and start_value is not None and end_value < start_value:
        end_value = None
    duration = (
        (end_value - start_value) * 1000
        if start_value is not None and end_value is not None
        else None
    )
    try:
        return Timing(
            start=start_value,
            end=end_value,
            duration_ms=duration,
            provenance=TimingProvenance.SOURCE,
        )
    except ValueError:
        return None


def _message_timing(info: dict) -> Timing | None:
    time_data = _table(info.get("time"))
    return _trajectory_timing(time_data.get("created"), time_data.get("completed"))


def _part_timing(part: dict, fallback=None) -> Timing | None:
    state = _table(part.get("state"))
    time_data = _table(part.get("time")) or _table(state.get("time"))
    return _trajectory_timing(
        time_data.get("start") or time_data.get("created"),
        time_data.get("end") or time_data.get("completed"),
        fallback=fallback,
    )


def _trajectory_usage(info: dict) -> TrajectoryUsage | None:
    model = _trajectory_identifier(_opencode_model(info), "model")
    try:
        usage = _opencode_usage(info)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return TrajectoryUsage(model=model) if model is not None else None
    if usage is None:
        return TrajectoryUsage(model=model) if model is not None else None
    values = {
        name: max(0, value) if isinstance(value, int) else 0
        for name, value in (
            ("input_tokens", usage.input_tokens),
            ("output_tokens", usage.output_tokens),
            ("reasoning_tokens", usage.reasoning_output_tokens),
            ("cache_read_tokens", usage.cache_read_input_tokens),
            ("cache_write_tokens", usage.cache_creation_input_tokens),
        )
    }
    cost = usage.cost_usd if usage.cost_usd is not None and math.isfinite(usage.cost_usd) else None
    if cost is not None and cost < 0:
        cost = None
    return TrajectoryUsage(
        model=model or _trajectory_identifier(usage.model, "model"),
        request_id=_trajectory_identifier(usage.idempotency_key, "request"),
        cost_usd=cost,
        **values,
    )


def _opencode_source_key(db: Path) -> str:
    return hashlib.sha256(str(db.expanduser().resolve()).encode("utf-8")).hexdigest()[:32]


class _OpenCodeHistoryPageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
        # Returning the absolute path would leak a home directory into the index; None drops it.
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
    #: An open lozenge, distinguishable from the three already taken.
    icon = "\u25c7"
    #: A spelling that does not normalize is observed as nothing at all, so these are not cosmetic.
    aliases = ("open-code", "open_code", "OpenCode", "opencode-ai")
    resume_strategy = "fork"
    #: `-s` routes to session view, `--prompt` only on home screen — a prompt with `-s` is dropped.
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
        # OpenCode keeps all sessions in one global SQLite; this hook supplies a correlation fact.
        config["plugin"] = [plugin_path.resolve().as_uri()]
        argv = ["opencode"]
        if model:
            # opencode wants `provider/model`, not a bare model name. Passed through as given.
            argv += ["--model", model]
        if approval == "yolo":
            argv.append("--auto")
        if resume is not None:
            argv += ["-s", resume, "--fork"]
        elif prompt:
            argv += ["--prompt", prompt]
        files = {
            config_path: json.dumps(config, indent=2),
            plugin_path: _correlation_plugin(participant_id, receipt_path),
        }
        return LaunchPlan(
            argv=argv,
            env={"OPENCODE_CONFIG": str(config_path)},
            files=files,
        )

    def resume_launch_overlay(
        self,
        *,
        predecessor: Participant,
        trusted_session_owners: Sequence[Participant],
    ) -> ResumeLaunchOverlay:
        """Validate a predecessor's transcript domain against the OpenCode db.

        Conditional: a predecessor with no domain is the normal case for
        OpenCode and returns an empty overlay. A predecessor with a domain is
        validated against the expected ``opencode://`` URI, reusing the same
        exact-equality check the bind path already enforces.
        """
        if predecessor.transcript_domain is None:
            return ResumeLaunchOverlay()
        expected = f"opencode://{self.observer.db.resolve()}"  # type: ignore[attr-defined]
        if predecessor.transcript_domain != expected:
            raise BadRequest(
                f"cannot resume OpenCode session: predecessor transcript domain "
                f"{predecessor.transcript_domain!r} does not match the OpenCode "
                f"observation domain {expected!r}"
            )
        return ResumeLaunchOverlay(transcript_domain=expected)

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
        domain: str | None = None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        if not self.db.exists() or not cwd:
            return []
        if domain is not None and domain != f"opencode://{self.db.resolve()}":
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
        #: message id -> role. Filled from the event stream; for skipped messages, `message` table.
        self._roles: dict[str, str] = {}
        #: message id -> {part id: text}. Insertion-ordered; joining reassembles a multi-part reply.
        self._text: dict[str, dict[str, str]] = {}
        #: call id -> last status seen, so one tool call yields one TOOL_CALL and one TOOL_RESULT.
        self._tools: dict[str, str] = {}
        #: message id -> time of the last part. Without this a reply stamps start, not stop.
        self._stamp: dict[str, float] = {}
        self._finished: set[str] = set()
        self._said: set[str] = set()
        self._trajectory_revisions: dict[str, int] = {}
        self._trajectory_signatures: dict[str, TrajectoryFact] = {}

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
        self._trajectory_revisions.clear()
        self._trajectory_signatures.clear()

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
        self._trajectory_revisions.clear()
        self._trajectory_signatures.clear()

    async def history(self, *, last_n: int) -> History:
        return await asyncio.to_thread(self._history, last_n)

    async def history_page(
        self,
        *,
        before: str | None = None,
        limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
    ) -> HistoryPage:
        if type(limit) is not int or limit <= 0:
            return HistoryPage(
                error_code="invalid_limit", error="history page limit must be positive"
            )
        limit = min(limit, TRAJECTORY_PAGE_RECORD_LIMIT)
        return await asyncio.to_thread(self._history_page, before, limit)

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
            return self._source_unavailable_batch("OpenCode database is unavailable")
        try:
            if self._session is None:
                found = self._locate(conn, pinned=True)
                if found:
                    return self._attach(conn, found)
                if self._trusted_known_location():
                    return self._identity_lost_batch(
                        f"trusted transcript pin {self._known_location!r} no longer exists"
                    )
                return self._correlation_problem(conn) or Batch(waiting=True)
            return self._drain(conn)
        except sqlite3.Error as exc:
            # Read-only under a live writer; lock/transient is source failure, not identity loss.
            logger.debug("reading the opencode database failed", exc_info=True)
            return self._source_unavailable_batch(f"reading OpenCode database failed: {exc}")

    def _refresh(self) -> Batch:
        """Propose the newest session for this directory if there is one.

        The pinned session id is ignored, for the reason `TranscriptSource`
        ignores it: a human can start a fresh session inside the same pane, and
        the id we stored names one that will never grow again.
        """
        self._require_decision()
        if self._receipt is None:
            # A global cwd scan finds "newer", not "mine"; legacy sources keep accepted session.
            return Batch()
        conn = self._open()
        if conn is None:
            return self._source_unavailable_batch("OpenCode database is unavailable")
        try:
            found = self._locate(conn, pinned=False)
            if found is None or found == self._session:
                return Batch()
            logger.info("opencode session changed: %s -> %s", self._session, found)
            return self._attach(conn, found)
        except sqlite3.Error as exc:
            logger.debug("relocating the opencode session failed", exc_info=True)
            return self._source_unavailable_batch(f"reading OpenCode database failed: {exc}")

    def _history(self, last_n: int) -> History:
        """Rebuild the conversation from `message` and `part`.

        Those tables hold current state, so this never sees the empty-then-full
        intermediates the event stream carries. Read independently of the poll
        cursor: the caller is usually a short-lived source of its own.
        """
        conn = self._open()
        if conn is None:
            return History(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error="OpenCode database is unavailable",
                pinned=self._trusted_known_location(),
            )
        pinned_sid = None
        if self._known_location and self._known_location.startswith("opencode://"):
            pinned_sid = self._known_location.removeprefix("opencode://") or None
        pinned = self._session is None and pinned_sid is not None
        try:
            if (
                pinned_sid is not None
                and self._trusted_known_location()
                and not self._session_exists(conn, pinned_sid)
            ):
                return History(
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                    error=f"trusted transcript pin {self._known_location!r} no longer exists",
                    pinned=True,
                )
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
        except sqlite3.Error as exc:
            logger.debug("reading opencode history failed", exc_info=True)
            return History(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"reading OpenCode database failed: {exc}",
                pinned=pinned,
            )
        events = [event for event in events if not event.usage_only]
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

    def _history_page(self, before: str | None, limit: int) -> HistoryPage:
        if not self._db.exists():
            return HistoryPage(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error="OpenCode database is unavailable",
            )
        try:
            conn = sqlite3.connect(f"file:{self._db}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            return HistoryPage(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"reading OpenCode database failed: {exc}",
            )
        try:
            return self._history_page_with_connection(conn, before, limit)
        except sqlite3.Error as exc:
            logger.debug("reading opencode history page failed", exc_info=True)
            return HistoryPage(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"reading OpenCode database failed: {exc}",
            )
        finally:
            conn.close()

    def _history_page_with_connection(  # noqa: PLR0912, PLR0915
        self, conn: sqlite3.Connection, before: str | None, limit: int
    ) -> HistoryPage:
        sid = self._history_session(conn)
        pinned = self._known_location is not None
        if sid is None:
            if before is not None:
                return HistoryPage(
                    error_code="history_cursor_invalid",
                    error="history cursor session is unavailable",
                    pinned=pinned,
                )
            return HistoryPage(pinned=pinned)
        try:
            stat = self._db.stat()
        except OSError as exc:
            return HistoryPage(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"OpenCode database is unavailable: {exc}",
                pinned=pinned,
            )
        identity = {"dev": int(stat.st_dev), "ino": int(stat.st_ino), "size": int(stat.st_size)}
        boundary: tuple[int | float, str, str] | None = None
        if before is not None:
            try:
                cursor = self._decode_history_cursor(before)
                boundary = self._validate_history_cursor(conn, cursor, sid, identity)
            except _OpenCodeHistoryPageError as exc:
                return HistoryPage(
                    error_code=exc.code,
                    error=str(exc),
                    pinned=pinned,
                )
        params: list[object] = [sid]
        sql = (
            "SELECT id, time_created, time_updated, data FROM message "
            "WHERE session_id = ? AND time_created IS NOT NULL"
        )
        if boundary is not None:
            created, message_id, _fingerprint = boundary
            sql += " AND (time_created < ? OR (time_created = ? AND id < ?))"
            params.extend((created, created, message_id))
        sql += " ORDER BY time_created DESC, id DESC LIMIT ?"
        params.append(limit + 1)
        rows = conn.execute(sql, params).fetchall()
        selected: list[tuple[object, object, object, object]] = []
        selected_output: list[tuple[tuple[Event, ...], tuple[TrajectoryFact, ...], int]] = []
        event_count = 0
        fact_count = 0
        has_more = False
        for row_index, row in enumerate(rows):
            if row_index >= limit:
                has_more = True
                break
            message_id, created, updated, raw = row
            key = self._history_row_key(created, message_id)
            if key is None:
                continue
            parts, parts_truncated = self._history_parts(conn, sid, str(message_id), limit)
            if parts_truncated:
                return HistoryPage(
                    error_code="history_record_too_large",
                    error="one OpenCode message has too many parts for the history page limit",
                    pinned=pinned,
                )
            info = _loads(raw)
            if not isinstance(info.get("id"), str):
                info["id"] = str(message_id)
            message_events = tuple(
                event
                for event in self._replay(info, [_loads(part[3]) for part in parts])
                if not event.usage_only
            )
            message_facts = tuple(
                self._stored_facts_for_message(
                    info,
                    parts,
                    raw_index=self._history_coordinate(created),
                    message_revision=self._stored_revision(info, updated, created),
                )
            )
            if len(message_events) > limit or len(message_facts) > limit:
                return HistoryPage(
                    error_code="history_record_too_large",
                    error="one OpenCode message exceeds the history page limit",
                    pinned=pinned,
                )
            if event_count + len(message_events) > limit or fact_count + len(message_facts) > limit:
                has_more = True
                break
            selected.append(row)
            selected_output.append(
                (message_events, message_facts, self._history_coordinate(created))
            )
            event_count += len(message_events)
            fact_count += len(message_facts)
        if not selected:
            return HistoryPage(
                location=f"opencode://{sid}",
                pinned=pinned,
                provenance=self._attachment_provenance(sid),
            )
        newest = selected[0]
        oldest = selected[-1]
        events: list[Event] = []
        facts: list[TrajectoryFact] = []
        for message_events, message_facts, coordinate in reversed(selected_output):
            events.extend(replace(event, raw_index=coordinate) for event in message_events)
            facts.extend(message_facts)
        newest_cursor = self._encode_history_cursor(sid, identity, newest)
        older_cursor = self._encode_history_cursor(sid, identity, oldest) if has_more else None
        return HistoryPage(
            location=f"opencode://{sid}",
            events=events,
            trajectory=facts,
            trajectory_events=(),
            cursor=newest_cursor,
            older_cursor=older_cursor,
            has_older=has_more,
            provenance=self._attachment_provenance(sid),
            pinned=pinned,
        )

    def _history_session(self, conn: sqlite3.Connection) -> str | None:
        if self._session is not None:
            return self._session if self._session_exists(conn, self._session) else None
        if self._receipt is not None:
            sid = self._read_receipt()
            if sid is not None and self._session_exists(conn, sid):
                return sid
        pinned = self._pinned_sid()
        if pinned is not None and self._trusted_known_location():
            return pinned if self._session_exists(conn, pinned) else None
        if self._session_id is not None and self._session_exists(conn, self._session_id):
            return self._session_id
        if not self._cwd:
            return None
        args: list[object] = [str(Path(self._cwd).resolve())]
        sql = "SELECT id FROM session WHERE directory = ? AND parent_id IS NULL"
        if self._after is not None:
            sql += " AND time_created >= ?"
            args.append(int(self._after * 1000))
        row = conn.execute(sql + " ORDER BY time_created DESC LIMIT 1", args).fetchone()
        return str(row[0]) if row is not None else None

    @staticmethod
    def _history_row_key(created, message_id) -> tuple[int | float, str, str] | None:
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            return None
        if not math.isfinite(created):
            return None
        if not isinstance(message_id, str) or not message_id:
            return None
        return created, message_id, ""

    @staticmethod
    def _history_revision(updated, created) -> int:
        value = (
            updated
            if isinstance(updated, (int, float)) and not isinstance(updated, bool)
            else created
        )
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            return 0
        return max(0, int(value))

    @classmethod
    def _history_coordinate(cls, created) -> int:
        return cls._history_revision(created, 0)

    @classmethod
    def _stored_revision(cls, data: dict, updated, created) -> int:
        persisted = cls._history_revision(updated, created)
        revision = data.get("revision")
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
            return max(revision, persisted)
        return persisted

    @staticmethod
    def _history_fingerprint(updated, raw) -> str:
        if isinstance(raw, bytes):
            encoded = raw
        elif isinstance(raw, str):
            encoded = raw.encode("utf-8", errors="replace")
        else:
            encoded = repr(raw).encode("utf-8", errors="replace")
        return hashlib.sha256(str(updated).encode("utf-8") + b"\0" + encoded).hexdigest()

    def _encode_history_cursor(
        self, sid: str, identity: dict[str, int], row: Sequence[object]
    ) -> str:
        message_id, created, updated, raw = row
        key = self._history_row_key(created, message_id)
        if key is None:
            raise ValueError("cannot encode an invalid OpenCode history boundary")
        payload = {
            "v": 1,
            "source": "opencode",
            "db": _opencode_source_key(self._db),
            "session": sid,
            "identity": identity,
            "boundary": [key[0], key[1]],
            "revision": self._history_revision(updated, created),
            "fingerprint": self._history_fingerprint(updated, raw),
        }
        encoded = (
            base64.urlsafe_b64encode(
                json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            )
            .decode("ascii")
            .rstrip("=")
        )
        cursor = "oc1." + encoded
        if len(cursor.encode("utf-8")) > TRAJECTORY_CURSOR_MAX_BYTES:
            raise ValueError("OpenCode history cursor exceeds its size limit")
        return cursor

    @staticmethod
    def _decode_history_cursor(cursor: str) -> dict[str, object]:
        if not isinstance(cursor, str) or not cursor.startswith("oc1."):
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor is not valid for OpenCode"
            )
        try:
            encoded_length = len(cursor.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor is malformed"
            ) from exc
        if encoded_length > TRAJECTORY_CURSOR_MAX_BYTES:
            raise _OpenCodeHistoryPageError("history_cursor_invalid", "history cursor is too large")
        try:
            raw = base64.urlsafe_b64decode(cursor[4:] + "=" * (-len(cursor[4:]) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except _OpenCodeHistoryPageError:
            raise
        except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor is malformed"
            ) from exc
        if not isinstance(payload, dict):
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor payload is malformed"
            )
        required = {
            "v",
            "source",
            "db",
            "session",
            "identity",
            "boundary",
            "revision",
            "fingerprint",
        }
        if set(payload) != required or payload.get("v") != 1 or payload.get("source") != "opencode":
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor does not belong to OpenCode"
            )
        return payload

    def _validate_history_cursor(
        self,
        conn: sqlite3.Connection,
        cursor: dict[str, object],
        sid: str,
        identity: dict[str, int],
    ) -> tuple[int | float, str, str]:
        if cursor.get("db") != _opencode_source_key(self._db) or cursor.get("session") != sid:
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor belongs to another source or session"
            )
        found_identity = cursor.get("identity")
        valid_identity = False
        if isinstance(found_identity, dict):
            found_size = found_identity.get("size")
            valid_identity = (
                found_identity.get("dev") == identity["dev"]
                and found_identity.get("ino") == identity["ino"]
                and type(found_size) is int
            )
        if not valid_identity:
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "OpenCode database identity changed"
            )
        boundary = cursor.get("boundary")
        if (
            not isinstance(boundary, list)
            or len(boundary) != 2
            or isinstance(boundary[0], bool)
            or not isinstance(boundary[0], (int, float))
            or not math.isfinite(boundary[0])
            or not isinstance(boundary[1], str)
            or not boundary[1]
        ):
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor boundary is malformed"
            )
        created, message_id = boundary
        row = conn.execute(
            "SELECT time_updated, data FROM message WHERE session_id = ? "
            "AND time_created = ? AND id = ?",
            (sid, created, message_id),
        ).fetchone()
        if row is None:
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "OpenCode history boundary no longer exists"
            )
        if cursor.get("revision") != self._history_revision(row[0], created):
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "OpenCode history boundary was updated"
            )
        if cursor.get("fingerprint") != self._history_fingerprint(row[0], row[1]):
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "OpenCode history boundary was updated"
            )
        return created, message_id, str(cursor["fingerprint"])

    def _history_parts(
        self, conn: sqlite3.Connection, sid: str, message_id: str, limit: int
    ) -> tuple[list[tuple[object, object, object, object]], bool]:
        rows = conn.execute(
            "SELECT id, time_created, time_updated, data FROM part "
            "WHERE message_id = ? AND session_id = ? "
            "ORDER BY time_created, id LIMIT ?",
            (message_id, sid, limit + 1),
        )
        found = list(rows)
        truncated = len(found) > limit
        found = found[:limit]
        found.sort(
            key=lambda row: (
                row[1] if isinstance(row[1], (int, float)) and not isinstance(row[1], bool) else 0,
                str(row[0]),
            )
        )
        return found, truncated

    def _stored_fact(
        self,
        *,
        kind: TrajectoryKind,
        summary: str,
        status: TrajectoryStatus,
        native_id: str | None,
        fallback_id: str | None,
        revision: int,
        raw_index: int,
        event_ordinal: int,
        turn_id: str | None = None,
        step_id: str | None = None,
        call_id: str | None = None,
        parent_call_id: str | None = None,
        timing: Timing | None = None,
        usage: TrajectoryUsage | None = None,
        details: Sequence[DetailField] = (),
    ) -> TrajectoryFact:
        native = _trajectory_identifier(native_id, "native")
        if native is None:
            native = _trajectory_identifier(fallback_id, "fallback")
        return TrajectoryFact(
            kind=kind,
            lane=_trajectory_lane(kind),
            source="opencode",
            summary=summary,
            status=status,
            native_id=native,
            revision=max(0, revision),
            raw_index=max(0, raw_index),
            event_ordinal=max(0, event_ordinal),
            turn_id=_trajectory_identifier(turn_id, "turn"),
            step_id=_trajectory_identifier(step_id, "step"),
            call_id=_trajectory_identifier(call_id, "call"),
            parent_call_id=_trajectory_identifier(parent_call_id, "parent-call"),
            timing=timing,
            usage=usage,
            details=tuple(details),
        )

    def _stored_facts_for_message(
        self,
        info: dict,
        parts: Sequence[tuple[object, object, object, object]],
        *,
        raw_index: int,
        message_revision: int,
    ) -> list[TrajectoryFact]:
        facts: list[TrajectoryFact] = []
        mid = _trajectory_string(info.get("id"))
        role = info.get("role")
        finish = info.get("finish")
        timing = _message_timing(info)
        usage = _trajectory_usage(info)
        ordinal = 0
        for part_id, created, updated, raw in parts:
            part = _loads(raw)
            if not isinstance(part.get("id"), str):
                part["id"] = str(part_id)
            part_facts = self._stored_facts_for_part(
                info,
                part,
                revision=self._stored_revision(part, updated, created),
                raw_index=raw_index,
                ordinal_base=ordinal,
                timing=timing,
                usage=usage,
            )
            facts.extend(part_facts)
            ordinal += max(1, len(part_facts))
        if not facts and role in ("user", "system", "developer"):
            content = _trajectory_text(info.get("content"))
            if content:
                facts.append(
                    self._stored_fact(
                        kind=TrajectoryKind.USER if role == "user" else TrajectoryKind.SYSTEM,
                        summary=content,
                        status=TrajectoryStatus.COMPLETED,
                        native_id=mid or None,
                        fallback_id=None,
                        revision=message_revision,
                        raw_index=raw_index,
                        event_ordinal=0,
                        turn_id=mid or None,
                        timing=timing,
                    )
                )
        if not facts and role == "assistant" and (finish or usage is not None):
            facts.append(
                self._stored_fact(
                    kind=TrajectoryKind.ASSISTANT,
                    summary="",
                    status=self._finish_status(finish),
                    native_id=mid or None,
                    fallback_id=None,
                    revision=message_revision,
                    raw_index=raw_index,
                    event_ordinal=0,
                    turn_id=mid or None,
                    timing=timing,
                    usage=usage,
                )
            )
        return facts

    def _stored_facts_for_part(
        self,
        info: dict,
        part: dict,
        *,
        revision: int,
        raw_index: int,
        ordinal_base: int,
        timing: Timing | None,
        usage: TrajectoryUsage | None,
    ) -> list[TrajectoryFact]:
        mid = _trajectory_string(info.get("id"))
        role = info.get("role")
        ptype = part.get("type")
        part_id = part.get("id") if isinstance(part.get("id"), str) else None
        fallback = part_id if isinstance(part_id, str) else None
        if ptype == "text":
            text = _trajectory_string(part.get("text"))
            if role == "assistant":
                kind = TrajectoryKind.ASSISTANT
                status = self._finish_status(info.get("finish"))
                fact_usage = usage
                fact_timing = timing or _part_timing(part)
            elif role == "user":
                kind = TrajectoryKind.USER
                status = TrajectoryStatus.COMPLETED
                fact_usage = None
                fact_timing = timing or _part_timing(part)
            elif role in ("system", "developer"):
                kind = TrajectoryKind.SYSTEM
                status = TrajectoryStatus.COMPLETED
                fact_usage = None
                fact_timing = timing or _part_timing(part)
            else:
                return []
            return [
                self._stored_fact(
                    kind=kind,
                    summary=text,
                    status=status,
                    native_id=part_id,
                    fallback_id=fallback,
                    revision=revision,
                    raw_index=raw_index,
                    event_ordinal=ordinal_base,
                    turn_id=mid or None,
                    timing=fact_timing,
                    usage=fact_usage,
                )
            ]
        if ptype in ("reasoning", "thinking"):
            text = _trajectory_string(part.get("text"))
            part_timing = _part_timing(part)
            status = (
                TrajectoryStatus.COMPLETED
                if part_timing is not None and part_timing.end is not None
                else self._finish_status(info.get("finish"))
            )
            return [
                self._stored_fact(
                    kind=TrajectoryKind.REASONING,
                    summary=text,
                    status=status,
                    native_id=part_id,
                    fallback_id=fallback,
                    revision=revision,
                    raw_index=raw_index,
                    event_ordinal=ordinal_base,
                    turn_id=mid or None,
                    timing=part_timing,
                )
            ]
        if ptype in ("context", "system"):
            text = _trajectory_text(part.get("text") or part.get("content"))
            return [
                self._stored_fact(
                    kind=TrajectoryKind.SYSTEM if ptype == "system" else TrajectoryKind.CONTEXT,
                    summary=text,
                    status=TrajectoryStatus.COMPLETED,
                    native_id=part_id,
                    fallback_id=fallback,
                    revision=revision,
                    raw_index=raw_index,
                    event_ordinal=ordinal_base,
                    turn_id=mid or None,
                    timing=_part_timing(part),
                )
            ]
        if ptype != "tool":
            return []
        state = _table(part.get("state"))
        state_status = state.get("status")
        call = part.get("callID") or part.get("id")
        call_id = call if isinstance(call, str) else None
        tool_name = part.get("tool") if isinstance(part.get("tool"), str) else None
        parent = part.get("parentCallID") or state.get("parentCallID")
        parent_id = parent if isinstance(parent, str) else None
        details = [
            value
            for value in (
                _trajectory_detail("tool", tool_name),
                _trajectory_detail("arguments", state.get("input"), format=ContentFormat.JSON),
            )
            if value is not None
        ]
        facts = [
            self._stored_fact(
                kind=TrajectoryKind.TOOL_CALL,
                summary=tool_name or "",
                status=self._tool_status(state_status),
                native_id=call_id,
                fallback_id=fallback,
                revision=revision,
                raw_index=raw_index,
                event_ordinal=ordinal_base,
                turn_id=mid or None,
                call_id=call_id,
                parent_call_id=parent_id,
                timing=_part_timing(part),
                details=details,
            )
        ]
        if state_status in ("completed", "error"):
            result = _tool_output(state)
            result_detail = _trajectory_detail("result", result)
            facts.append(
                self._stored_fact(
                    kind=TrajectoryKind.TOOL_RESULT,
                    summary=result,
                    status=(
                        TrajectoryStatus.ERROR
                        if state_status == "error"
                        else TrajectoryStatus.COMPLETED
                    ),
                    native_id=f"{call_id}:result" if call_id else None,
                    fallback_id=f"{fallback}:result" if fallback else None,
                    revision=revision,
                    raw_index=raw_index,
                    event_ordinal=ordinal_base + 1,
                    turn_id=mid or None,
                    call_id=call_id,
                    parent_call_id=parent_id,
                    timing=_part_timing(part),
                    details=(result_detail,) if result_detail is not None else (),
                )
            )
        return facts

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
        usage = _opencode_usage(info)
        if text or turn_end or usage is not None:
            out.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    text=whole(text),
                    raw_text=text,
                    ts=ts,
                    turn_end=turn_end,
                    turn_id=info.get("id") or None,
                    usage=usage,
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
        # A receipt is an exact process-local claim; outranks stored id from the ambiguous fallback.
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
        pinned_sid = self._pinned_sid()
        if pinned and pinned_sid is not None and self._trusted_known_location():
            row = conn.execute(
                "SELECT id FROM session WHERE id = ? AND parent_id IS NULL",
                (pinned_sid,),
            ).fetchone()
            self._located_exact = row is not None
            self._located_receipt_sid = None
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
        # Legacy discovery is a candidate; reducer rejects when same-cwd competitor makes ambiguous.
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
        # Ready marker may wait in a promptless pane; once a root session exists, it should publish.
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

    def _pinned_sid(self) -> str | None:
        if self._known_location and self._known_location.startswith("opencode://"):
            return self._known_location.removeprefix("opencode://") or None
        return None

    def _trusted_known_location(self) -> bool:
        return self._pinned_sid() is not None and is_trusted_provenance(
            self._known_location_provenance
        )

    @staticmethod
    def _identity_lost_batch(reason: str) -> Batch:
        return Batch(
            waiting=True,
            error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
            error=reason,
        )

    @staticmethod
    def _source_unavailable_batch(reason: str) -> Batch:
        return Batch(
            waiting=True,
            error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
            error=reason,
        )

    @staticmethod
    def _session_exists(conn: sqlite3.Connection, sid: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM session WHERE id = ? AND parent_id IS NULL",
                (sid,),
            ).fetchone()
            is not None
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
        trajectory: list[TrajectoryFact] = []
        for seq, kind, raw in rows:
            self._cursor = seq
            translated, facts = self._translate_with_trajectory(conn, kind, _loads(raw), seq)
            events.extend(translated)
            trajectory.extend(facts)
        # Rows consumed is progress: session.updated through a turn, else rescue fires mid-turn.
        return Batch(
            events=events,
            progressed=True,
            trajectory=trajectory,
            trajectory_events=(),
        )

    def _translate(
        self, conn: sqlite3.Connection, kind: str, payload: dict, seq: int
    ) -> list[Event]:
        return self._translate_with_trajectory(conn, kind, payload, seq)[0]

    def _translate_with_trajectory(
        self, conn: sqlite3.Connection, kind: str, payload: dict, seq: int
    ) -> tuple[list[Event], list[TrajectoryFact]]:
        if kind == "message.part.updated.1":
            part = payload.get("part")
            message_id = part.get("messageID") if isinstance(part, dict) else None
            coordinate = self._message_coordinate(conn, message_id, seq)
            events = self._on_part(conn, payload, seq)
            return events, self._trajectory_for_part(conn, payload, seq, raw_index=coordinate)
        if kind == "message.updated.1":
            info = payload.get("info")
            message_id = info.get("id") if isinstance(info, dict) else None
            coordinate = self._message_coordinate(conn, message_id, seq)
            events = self._on_message(payload, seq)
            facts = self._trajectory_for_message(conn, payload, seq, raw_index=coordinate)
            if isinstance(info, dict):
                finish = info.get("finish")
                if finish and finish != STEP_FINISH and isinstance(message_id, str):
                    self._text.pop(message_id, None)
            return events, facts
        # session.created / session.updated: progress, not conversation.
        return [], []

    def _live_fact(
        self,
        *,
        kind: TrajectoryKind,
        summary: str,
        status: TrajectoryStatus,
        native_id: str | None,
        fallback_id: str | None,
        raw_index: int,
        event_ordinal: int,
        turn_id: str | None = None,
        step_id: str | None = None,
        call_id: str | None = None,
        parent_call_id: str | None = None,
        timing: Timing | None = None,
        usage: TrajectoryUsage | None = None,
        details: Sequence[DetailField] = (),
        revision_hint: int | None = None,
    ) -> TrajectoryFact | None:
        native = _trajectory_identifier(native_id, "native")
        if native is None:
            native = _trajectory_identifier(fallback_id, "fallback")
        candidate = TrajectoryFact(
            kind=kind,
            lane=_trajectory_lane(kind),
            source="opencode",
            summary=summary,
            status=status,
            native_id=native,
            raw_index=max(0, raw_index),
            event_ordinal=max(0, event_ordinal),
            turn_id=_trajectory_identifier(turn_id, "turn"),
            step_id=_trajectory_identifier(step_id, "step"),
            call_id=_trajectory_identifier(call_id, "call"),
            parent_call_id=_trajectory_identifier(parent_call_id, "parent-call"),
            timing=timing,
            usage=usage,
            details=tuple(details),
        )
        key = native or f"fallback:{candidate.raw_index}:{candidate.event_ordinal}:{kind.value}"
        previous = self._trajectory_signatures.get(key)
        if previous is not None:
            comparable = replace(
                candidate,
                raw_index=previous.raw_index,
                event_ordinal=previous.event_ordinal,
            )
            if previous == comparable:
                return None
        revision = max(self._trajectory_revisions.get(key, -1) + 1, revision_hint or 0)
        self._trajectory_revisions[key] = revision
        self._trajectory_signatures[key] = candidate
        return replace(candidate, revision=revision)

    def _live_revision(
        self,
        conn: sqlite3.Connection,
        table: str,
        record_id: str | None,
        *fallbacks: object,
    ) -> int:
        values = list(fallbacks)
        if table not in {"message", "part"}:
            raise ValueError("unsupported OpenCode revision table")
        if record_id:
            query = (
                "SELECT time_updated, time_created FROM message WHERE id = ?"
                if table == "message"
                else "SELECT time_updated, time_created FROM part WHERE id = ?"
            )
            row = conn.execute(
                query,
                (record_id,),
            ).fetchone()
            if row is not None:
                values[:0] = row
        revisions = [self._history_revision(value, 0) for value in values]
        return max(revisions, default=0)

    def _message_coordinate(
        self, conn: sqlite3.Connection, message_id: object, fallback: int
    ) -> int:
        if isinstance(message_id, str) and message_id:
            row = conn.execute(
                "SELECT time_created FROM message WHERE id = ?", (message_id,)
            ).fetchone()
            if row is not None:
                return self._history_coordinate(row[0])
        return max(0, fallback)

    @staticmethod
    def _finish_status(finish: object) -> TrajectoryStatus:
        if not finish:
            return TrajectoryStatus.RUNNING
        return TrajectoryStatus.PARTIAL if finish == STEP_FINISH else TrajectoryStatus.COMPLETED

    @staticmethod
    def _tool_status(status: object) -> TrajectoryStatus:
        if status == "pending":
            return TrajectoryStatus.PENDING
        if status == "running":
            return TrajectoryStatus.RUNNING
        if status == "completed":
            return TrajectoryStatus.COMPLETED
        if status == "error":
            return TrajectoryStatus.ERROR
        return TrajectoryStatus.UNKNOWN

    def _trajectory_for_part(  # noqa: PLR0912, PLR0915
        self, conn: sqlite3.Connection, payload: dict, seq: int, *, raw_index: int
    ) -> list[TrajectoryFact]:
        part = payload.get("part")
        if not isinstance(part, dict):
            return []
        mid = part.get("messageID")
        message_id = mid if isinstance(mid, str) else ""
        role = self._role(conn, message_id) if message_id else None
        ptype = part.get("type")
        timing = _part_timing(part)
        part_id = part.get("id")
        fallback = part_id if isinstance(part_id, str) else None
        revision_hint = self._live_revision(
            conn,
            "part",
            fallback,
            payload.get("time"),
            seq,
        )
        step_id = part.get("stepID") or part.get("stepId")
        if ptype == "text":
            if role == "assistant":
                kind = TrajectoryKind.ASSISTANT
                status = TrajectoryStatus.RUNNING
            elif role == "user":
                kind = TrajectoryKind.USER
                status = TrajectoryStatus.COMPLETED
            elif role in ("system", "developer"):
                kind = TrajectoryKind.SYSTEM
                status = TrajectoryStatus.COMPLETED
            else:
                return []
            text = _trajectory_string(part.get("text"))
            fact = self._live_fact(
                kind=kind,
                summary=text,
                status=status,
                native_id=part_id,
                fallback_id=fallback,
                raw_index=raw_index,
                event_ordinal=0,
                turn_id=message_id or None,
                step_id=step_id if isinstance(step_id, str) else None,
                timing=timing,
                revision_hint=revision_hint,
            )
            return [fact] if fact is not None else []
        if ptype in ("reasoning", "thinking"):
            text = _trajectory_string(part.get("text"))
            status = (
                TrajectoryStatus.COMPLETED
                if timing is not None and timing.end is not None
                else TrajectoryStatus.RUNNING
            )
            fact = self._live_fact(
                kind=TrajectoryKind.REASONING,
                summary=text,
                status=status,
                native_id=part_id,
                fallback_id=fallback,
                raw_index=raw_index,
                event_ordinal=0,
                turn_id=message_id or None,
                step_id=step_id if isinstance(step_id, str) else None,
                timing=timing,
                revision_hint=revision_hint,
            )
            return [fact] if fact is not None else []
        if ptype in ("context", "system"):
            text = _trajectory_text(part.get("text") or part.get("content"))
            fact = self._live_fact(
                kind=TrajectoryKind.SYSTEM if ptype == "system" else TrajectoryKind.CONTEXT,
                summary=text,
                status=TrajectoryStatus.COMPLETED,
                native_id=part_id,
                fallback_id=fallback,
                raw_index=raw_index,
                event_ordinal=0,
                turn_id=message_id or None,
                step_id=step_id if isinstance(step_id, str) else None,
                timing=timing,
                revision_hint=revision_hint,
            )
            return [fact] if fact is not None else []
        if ptype != "tool":
            return []
        state = _table(part.get("state"))
        state_status = state.get("status")
        call = part.get("callID") or part.get("id")
        call_id = call if isinstance(call, str) else None
        tool_name = part.get("tool") if isinstance(part.get("tool"), str) else None
        parent = part.get("parentCallID") or state.get("parentCallID")
        parent_id = parent if isinstance(parent, str) else None
        details: list[DetailField] = []
        tool_detail = _trajectory_detail("tool", tool_name)
        if tool_detail is not None:
            details.append(tool_detail)
        input_detail = _trajectory_detail(
            "arguments", state.get("input"), format=ContentFormat.JSON
        )
        if input_detail is not None:
            details.append(input_detail)
        call_fact = self._live_fact(
            kind=TrajectoryKind.TOOL_CALL,
            summary=tool_name or "",
            status=self._tool_status(state_status),
            native_id=call_id,
            fallback_id=fallback,
            raw_index=raw_index,
            event_ordinal=0,
            turn_id=message_id or None,
            step_id=step_id if isinstance(step_id, str) else None,
            call_id=call_id,
            parent_call_id=parent_id,
            timing=timing,
            details=details,
            revision_hint=revision_hint,
        )
        facts = [call_fact] if call_fact is not None else []
        if state_status in ("completed", "error"):
            result = _tool_output(state)
            result_details: list[DetailField] = []
            result_detail = _trajectory_detail("result", result)
            if result_detail is not None:
                result_details.append(result_detail)
            result_fact = self._live_fact(
                kind=TrajectoryKind.TOOL_RESULT,
                summary=result,
                status=(
                    TrajectoryStatus.ERROR
                    if state_status == "error"
                    else TrajectoryStatus.COMPLETED
                ),
                native_id=f"{call_id}:result" if call_id else None,
                fallback_id=f"{fallback}:result" if fallback else None,
                raw_index=raw_index,
                event_ordinal=1,
                turn_id=message_id or None,
                step_id=step_id if isinstance(step_id, str) else None,
                call_id=call_id,
                parent_call_id=parent_id,
                timing=timing,
                details=result_details,
                revision_hint=revision_hint,
            )
            if result_fact is not None:
                facts.append(result_fact)
        return facts

    def _trajectory_for_message(
        self, conn: sqlite3.Connection, payload: dict, seq: int, *, raw_index: int
    ) -> list[TrajectoryFact]:
        info = payload.get("info")
        if not isinstance(info, dict):
            return []
        mid = _trajectory_string(info.get("id"))
        role = info.get("role")
        finish = info.get("finish")
        timing = _message_timing(info)
        usage = _trajectory_usage(info)
        time_data = _table(info.get("time"))
        revision_hint = self._live_revision(
            conn,
            "message",
            mid or None,
            time_data.get("updated"),
            time_data.get("completed"),
            time_data.get("created"),
            seq,
        )
        if role == "assistant":
            text_parts = self._text.get(mid, {})
            if text_parts:
                facts: list[TrajectoryFact] = []
                status = self._finish_status(finish)
                for ordinal, (part_id, part_text) in enumerate(text_parts.items()):
                    fact = self._live_fact(
                        kind=TrajectoryKind.ASSISTANT,
                        summary=part_text,
                        status=status,
                        native_id=part_id,
                        fallback_id=f"{mid}:text" if mid else None,
                        raw_index=raw_index,
                        event_ordinal=ordinal,
                        turn_id=mid or None,
                        timing=timing,
                        usage=usage,
                        revision_hint=revision_hint,
                    )
                    if fact is not None:
                        facts.append(fact)
                return facts
            if finish or usage is not None:
                fact = self._live_fact(
                    kind=TrajectoryKind.ASSISTANT,
                    summary="",
                    status=self._finish_status(finish),
                    native_id=mid or None,
                    fallback_id=None,
                    raw_index=raw_index,
                    event_ordinal=0,
                    turn_id=mid or None,
                    timing=timing,
                    usage=usage,
                    revision_hint=revision_hint,
                )
                return [fact] if fact is not None else []
            return []
        if role in ("user", "system", "developer"):
            content = _trajectory_text(info.get("content"))
            if not content:
                return []
            kind = TrajectoryKind.USER if role == "user" else TrajectoryKind.SYSTEM
            fact = self._live_fact(
                kind=kind,
                summary=content,
                status=TrajectoryStatus.COMPLETED,
                native_id=mid or None,
                fallback_id=None,
                raw_index=raw_index,
                event_ordinal=0,
                turn_id=mid or None,
                timing=timing,
                revision_hint=revision_hint,
            )
            return [fact] if fact is not None else []
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
            # `running` is the first status that carries `state.input`, so paths are available here.
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
        text = "".join(self._text.get(mid, {}).values())
        turn_end = finish != STEP_FINISH
        usage = _opencode_usage(info)
        if not text and not turn_end and usage is None:
            return []
        return [
            Event(
                kind=EventKind.ASSISTANT,
                text=clip(text),
                raw_text=text,
                ts=ts,
                turn_end=turn_end,
                turn_id=mid or None,
                raw_index=seq,
                usage=usage,
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


#: What the loader looks for. An instance, not the class (see docs/harness-plugins.md).
HARNESS = OpenCodeHarness()
