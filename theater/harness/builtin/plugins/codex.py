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
awaiting input because the trust dialog renders a `›` selection row, which
is the same glyph the idle composer uses. `screen_reading` checks the modal
markers before falling through to `is_idle_screen`, so the trust dialog
classifies as TRUST rather than PROMPT.

Transcript layout
-----------------
    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<local-ISO>-<session_id>.jsonl

Two independent traps in that name. The timestamp is *local* time while every
`timestamp` field inside the file is UTC, so the two are never comparable — time
filtering here goes through stat() and nothing else. The uuid suffix, on the
other hand, is exactly `session_meta.payload.session_id` (checked on every
transcript to hand), which makes a known session id a pure glob.

Which rollout is ours
---------------------
Codex mints its `ThreadId` internally and the public CLI accepts a session id
only on `resume` and `fork`, so a new interactive session cannot be launched
with an id we chose. Until a transcript is found, the participant therefore has
no session id at all, and discovery has nothing sharper than `session_meta.cwd`
plus a birth-time floor. Two agents in one directory both satisfy that, so the
reducer's collision guard refuses both — correctly, and at the cost of the
await and of `read_transcript`.

The exact channel is the process itself: codex holds its rollout open for the
lifetime of the session, so the file descriptors of the pane's codex process
name the transcript that belongs to it. That evidence survives a daemon
restart, is available before the agent has made a single MCP call, and changes
no Codex configuration — which is more than any of `CODEX_HOME` isolation, a
`SessionStart` hook receipt, or `_meta.threadId` on an MCP request can say.

It applies to spawned panes only. There the pane process *is* the CLI Theater
started, so the pid the registry holds names that session for as long as the
participant lives. An adopted pane runs a shell instead, and a shell outlives
what it ran: the codex under it now need not be the codex the participant was
adopted from, and no amount of counting processes can tell the difference. So
adopted panes get no proof and keep the behaviour they have always had. Giving
them proof means associating a participant with a *process* at adoption time
and keeping it — daemon state, and the daemon's to keep.

Three keys, then, in a deliberate order. A session id we were *given* — a
resume token, a launch receipt — is asked first: it names the file outright,
no second codex in the pane can confuse it, and it costs a glob instead of
three subprocesses. The process is asked next. A session id we merely *read
back* off a file comes last, behind the process, because it may itself be an
earlier guess: put it first and discovery re-derives the same wrong file
forever, with no way for proof to ever displace it.

When the process cannot be inspected — no `/proc`, no `lsof`, a rollout not
yet created, more than one open at once, or more than one codex in the pane to
choose between — discovery falls back to the cwd scan exactly as before and the
candidate is reported as heuristic. Nothing here decides what to do about that:
the reducer's guard is the one place that refuses a contested attachment, and
this adapter's job is only to say honestly how well it knows.

Proof is also offered on its own, through `proven_transcript`. A participant
bound before any of this existed carries a heuristic location that every later
poll takes before discovery is consulted, so it would stay contested for the
rest of its life; the source offers such a location to the proof channel, and
only to the proof channel, so a failed probe leaves it alone rather than
replacing it with a fresh guess.

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
import logging
import math
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from theater import proc
from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.harness.base import (
    APPROVALS,
    SERVER_NAME,
    Event,
    EventKind,
    EventPath,
    Harness,
    LaunchPlan,
    NativeChild,
    ResumeLaunchOverlay,
    TokenUsage,
    clipper,
    theater_binary,
)
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.harness.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
    TranscriptObserver,
)
from theater.harness.source import Source, TranscriptCandidate, TranscriptSource
from theater.models import BadRequest
from theater.provenance import TranscriptProvenance, normalize_provenance
from theater.trajectory.capabilities import TrajectoryCapabilities, TrajectoryFeature
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import (
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)
from theater.trajectory.records import Timing, TrajectoryUsage

if TYPE_CHECKING:
    from theater.models import Participant

logger = logging.getLogger("theater.harness.codex")

#: The composer prompt. A single glyph (U+203A), not the ASCII ">" that Claude Code uses.
PROMPT = "\u203a"

#: Present in the status bar while a turn runs. Codex keeps a persistent footer.
WORKING_MARKER = "esc to interrupt"

#: Approval overlay and MCP/auth prompts. NOT `to confirm`: the `/approvals` popup renders that.
APPROVAL_MARKER = "to cancel"

#: First-launch trust dialog. Whole-capture, not tail-scoped: body text above the rows.
TRUST_MARKER = "Do you trust the contents"

#: How far up from the bottom to look for the composer.
_SCREEN_TAIL_LINES = 5

#: `session_meta` is the first record and carries `cwd`; probed by reading exactly one line.
_CWD_PROBE_BYTES = 256 * 1024

#: Bound record reads during heuristic loss detection; only the newest few files are opened for cwd.
_LOSS_CANDIDATE_PROBES = 8

#: Filename is `rollout-<ISO with - separators>-<uuid>`. Anchoring on timestamp keeps uuid hyphens.
_STEM = re.compile(r"^rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(.+)$")

#: apply_patch hunks are delimited by these markers. Paths after them are repo-relative.
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)


def _apply_patch_paths(text: str) -> tuple[EventPath, ...]:
    """Extract file paths from an ``apply_patch`` tool input string.

    The apply_patch format is a structured patch grammar with explicit
    per-file markers (``*** Update File:``, ``*** Add File:``, ``*** Delete
    File:``), not prose or a shell command. The markers and their grammar are
    defined in codex-rs/apply-patch/src/parser.rs:39-41. Every hunk is a write
    — update, create, and delete are all mutations — so every path gets
    ``mode="write"``.

    A malformed input yields nothing rather than a partial guess: a wrong
    path in the touch index is worse than a missing one.
    """
    if not isinstance(text, str):
        return ()
    return tuple(
        EventPath(path=match.strip(), mode="write") for match in _PATCH_FILE_RE.findall(text)
    )


def _in_screen_tail(capture: str, marker: str) -> bool:
    """Whether any of the last few non-blank lines *ends with* *marker*.

    The approval footer is chrome the CLI always draws at the bottom of the
    modal, so searching the whole pane buys nothing — and matching the whole
    pane lets agent output (ordinary prose) impersonate the footer. Scoping
    to the same tail window ``is_idle_screen`` uses is necessary but not
    sufficient on its own: a real codex idle pane has agent output in three
    of the five scanned tail lines (see ``codex_idle.txt``), so the window
    unavoidably contains prose. The end-of-line anchor is the second guard:
    the footer is a whole line that ends with the marker, while prose
    containing the phrase virtually never ends a line with it. Neither the
    tail window nor the endswith test alone is enough; both are required.
    """
    lines = [line.strip() for line in capture.splitlines() if line.strip()]
    return any(line.endswith(marker) for line in lines[-_SCREEN_TAIL_LINES:])


def _resolve(path: Path) -> Path:
    """`Path.resolve`, but a path we cannot stat is not an error here.

    Every comparison in the correlation path is between a name the kernel gave
    us and a name a human configured, and on macOS those differ by `/private`
    for anything under a temporary directory. Resolving both sides is what
    makes them comparable; a file that vanished mid-probe just compares as
    itself.
    """
    try:
        return path.resolve()
    except OSError:
        return path


def _is_codex(comm: str) -> bool:
    """Whether a `ps` command column names the codex CLI.

    Compared on the basename: `ps -o comm` gives a bare `codex` for a plain
    install and an absolute path for some wrappers, and under Nix the image
    behind it is a `.codex-wrapped` shim the column never shows.
    """
    return comm.rsplit("/", 1)[-1] == CodexHarness.binary


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


def _turn_id(payload: dict) -> str | None:
    """The turn this record belongs to, as Codex names it.

    Stamped identically on `task_started` and on whichever record closes the
    turn, so the two ends of a turn are joinable without inference. Only read
    off the boundary records: the mid-turn `agent_message` and `user_message`
    events carry no turn_id at all, and inventing one for them by remembering
    the last `task_started` would mean holding state across lines, which
    parse() deliberately does not do.
    """
    tid = payload.get("turn_id")
    return tid if isinstance(tid, str) and tid else None


def _safe_trajectory_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")
    return value


def _trajectory_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return None
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        return None
    return value


def _stable_json(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError, UnicodeError):
        return json.dumps(str(value), ensure_ascii=True)


def _trajectory_detail(name: str, value: object, *, format: ContentFormat) -> DetailField:
    text = value if isinstance(value, str) else _stable_json(value)
    return DetailField.from_text(name, _safe_trajectory_text(text), format=format)


def _trajectory_int(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return 0


def _trajectory_float(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _trajectory_time(value: object) -> float | None:
    if isinstance(value, str):
        return _epoch(value)
    return _trajectory_float(value)


def _codex_duration(value: object) -> float | None:
    if isinstance(value, dict):
        seconds = _trajectory_float(value.get("secs"))
        nanos = _trajectory_float(value.get("nanos"))
        if seconds is not None and nanos is not None and seconds >= 0 and nanos >= 0:
            duration = seconds * 1000 + nanos / 1_000_000
            return duration if math.isfinite(duration) else None
        return None
    scalar_duration = _trajectory_float(value)
    return scalar_duration if scalar_duration is not None and scalar_duration >= 0 else None


def _codex_timing(record: dict, payload: dict, timestamp: float | None) -> Timing | None:
    values = (payload, record)
    start = next(
        (
            _trajectory_time(value.get(key))
            for value in values
            for key in ("started_at", "startedAt", "start_time", "startTime")
            if _trajectory_time(value.get(key)) is not None
        ),
        None,
    )
    end = next(
        (
            _trajectory_time(value.get(key))
            for value in values
            for key in ("completed_at", "completedAt", "end_time", "endTime")
            if _trajectory_time(value.get(key)) is not None
        ),
        None,
    )
    duration = next(
        (
            _codex_duration(value.get(key))
            for value in values
            for key in ("duration_ms", "durationMs", "duration")
            if _codex_duration(value.get(key)) is not None
        ),
        None,
    )
    if start is None and end is None and duration is None and timestamp is not None:
        start = timestamp
    if start is None and end is None and duration is None:
        return None
    if start is not None and end is not None and end < start:
        end = None
    return Timing(start=start, end=end, duration_ms=duration, provenance=TimingProvenance.SOURCE)


def _trajectory_status(value: object, default: TrajectoryStatus) -> TrajectoryStatus:
    if isinstance(value, TrajectoryStatus):
        return value
    if not isinstance(value, str):
        return default
    aliases = {
        "complete": TrajectoryStatus.COMPLETED,
        "completed": TrajectoryStatus.COMPLETED,
        "done": TrajectoryStatus.COMPLETED,
        "success": TrajectoryStatus.COMPLETED,
        "failed": TrajectoryStatus.ERROR,
        "failure": TrajectoryStatus.ERROR,
        "error": TrajectoryStatus.ERROR,
        "cancelled": TrajectoryStatus.CANCELLED,
        "canceled": TrajectoryStatus.CANCELLED,
        "aborted": TrajectoryStatus.INTERRUPTED,
        "interrupted": TrajectoryStatus.INTERRUPTED,
        "in_progress": TrajectoryStatus.RUNNING,
        "running": TrajectoryStatus.RUNNING,
        "partial": TrajectoryStatus.PARTIAL,
        "pending": TrajectoryStatus.PENDING,
    }
    return aliases.get(value.lower().replace("-", "_"), default)


def _codex_revision(record: dict, payload: dict) -> int:
    for value in (payload, record):
        for key in ("revision", "version"):
            candidate = _trajectory_int(value.get(key))
            if candidate or value.get(key) in (0, 0.0):
                return candidate
    return 0


def _codex_block_id(item_id: str | None, block: dict, ordinal: int) -> str | None:
    explicit = _trajectory_id(block.get("id"))
    if explicit is not None:
        return explicit
    if item_id is None:
        return None
    return item_id if ordinal == 0 else f"{item_id}:content:{ordinal}"


def _codex_scoped_id(value: str | None, suffix: str) -> str | None:
    return _trajectory_id(f"{value}:{suffix}") if value is not None else None


def _codex_content_text(value: object) -> str:
    if isinstance(value, str):
        return _safe_trajectory_text(value)
    if isinstance(value, list):
        text = "".join(
            _safe_trajectory_text(item.get("text"))
            for item in value
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
        if text:
            return text
    return _safe_trajectory_text(_stable_json(value)) if value is not None else ""


def _codex_trajectory_turn_id(payload: dict) -> str | None:
    direct = _trajectory_id(payload.get("turn_id") or payload.get("turnId"))
    if direct is not None:
        return direct
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(metadata, dict):
        return _trajectory_id(metadata.get("turn_id") or metadata.get("turnId"))
    return None


def _codex_usage(record: dict, payload: dict) -> TrajectoryUsage | None:
    info = payload.get("info") if payload.get("type") == "token_count" else None
    raw = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(raw, dict) and isinstance(info, dict):
        raw = info.get("total_token_usage")
    if not isinstance(raw, dict):
        raw = payload.get("usage") or payload.get("token_usage")
    if not isinstance(raw, dict):
        return None
    input_total = _trajectory_int(raw.get("input_tokens"))
    cache_read = _trajectory_int(raw.get("cached_input_tokens"))
    cache_write = _trajectory_int(raw.get("cache_write_input_tokens"))
    output_total = _trajectory_int(raw.get("output_tokens"))
    reasoning = _trajectory_int(raw.get("reasoning_output_tokens"))
    known = (
        input_total,
        cache_read,
        cache_write,
        output_total,
        reasoning,
    )
    if not any(known):
        return None
    model_value = None
    if isinstance(info, dict):
        model_value = info.get("model") or info.get("model_name")
    model_value = model_value or payload.get("model") or record.get("model")
    request_id = _trajectory_id(
        payload.get("request_id") or payload.get("requestId") or payload.get("turn_id")
    )
    cost = _trajectory_float(raw.get("cost_usd") or raw.get("costUSD"))
    return TrajectoryUsage(
        model=_trajectory_id(model_value),
        request_id=request_id,
        input_tokens=max(0, input_total - cache_read - cache_write),
        output_tokens=max(0, output_total - reasoning),
        reasoning_tokens=reasoning,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost if cost is None or cost >= 0 else None,
    )


class CodexHarness(Harness):
    name = "codex"
    binary = "codex"
    #: A filled ring. Not another asterisk-family glyph: `✻` is taken by Claude Code.
    icon = "\u25c9"
    #: A spelling that does not normalize is observed as nothing at all, so these are not cosmetic.
    aliases = ("codex-cli", "codex_cli", "openai-codex", "Codex")
    resume_strategy = "fork"

    def __init__(self, root: Path | None = None):
        #: The observer's business alone; nothing about launching depends on it.
        self.observer = CodexObserver(root=root)

    # ---- launching ------------------------------------------------------

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        resume: str | None = None,
    ) -> LaunchPlan:
        if approval not in APPROVALS:
            raise BadRequest(f"approval must be one of {', '.join(APPROVALS)}, got {approval!r}")
        command = json.dumps(theater_binary())
        args = json.dumps(["mcp", "--id", participant_id])
        # `codex fork <SESSION_ID>` preserves context under a fresh native session identity.
        argv = [
            "codex",
        ]
        if resume is not None:
            argv.append("fork")
            argv.append(resume)
        argv += [
            "-c",
            f"mcp_servers.{SERVER_NAME}.command={command}",
            "-c",
            f"mcp_servers.{SERVER_NAME}.args={args}",
        ]
        if model:
            argv += ["--model", model]
        if reasoning_effort:
            argv += ["-c", f"model_reasoning_effort={reasoning_effort}"]
        if approval == "yolo":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        elif approval == "edits":
            argv += ["-a", "on-request", "-s", "workspace-write"]
        else:
            argv += ["-a", "untrusted", "-s", "read-only"]
        if prompt:
            argv.append(prompt)
        return LaunchPlan(argv=argv)

    def resume_launch_overlay(
        self,
        *,
        predecessor: Participant,
        trusted_session_owners: Sequence[Participant],
    ) -> ResumeLaunchOverlay:
        """Validate a predecessor's transcript domain against the observer root.

        Conditional: a predecessor with no domain is the normal case for Codex
        and returns an empty overlay. A predecessor with a domain is a new
        explicit constraint — Codex does not enforce this at bind time, so
        this is a new check, not a reuse of an existing one.
        """
        if predecessor.transcript_domain is None:
            return ResumeLaunchOverlay()
        root = self.observer.root.resolve()  # type: ignore[attr-defined]
        declared = Path(predecessor.transcript_domain).resolve(strict=False)
        if declared != root:
            raise BadRequest(
                f"cannot resume Codex session: predecessor transcript domain "
                f"{declared!r} does not match the Codex observation root {root!r}"
            )
        return ResumeLaunchOverlay(transcript_domain=str(root))


class _CodexSource(TranscriptSource):
    """A codex transcript source whose exactness is decided per location.

    The flags `TranscriptSource` already understands are fixed when the source
    is built: either every candidate under this root has one owner, or the
    session id we were handed was itself exact. Neither describes codex, where
    the same source proves ownership on one poll — the process was holding the
    file — and can only guess on the next, because `lsof` is missing or the
    rollout does not exist yet. So the question is asked about the path.
    """

    def __init__(self, observer: CodexObserver, **kwargs) -> None:
        super().__init__(observer, **kwargs)
        #: Same as `self._observer`, renamed so `proved` is not a `TranscriptObserver` API.
        self._codex = observer

    def correlation_for(self, path: Path, session_id: str | None) -> str:
        if self._codex.proved(path):
            return str(TranscriptProvenance.PROVEN)
        return super().correlation_for(path, session_id)

    def commit_attachment(self) -> None:
        super().commit_attachment()
        # One fact in two places: source's flag labels, observer's decides which key to ask.
        self._codex._session_exact = self._session_provenance is TranscriptProvenance.EXACT


class CodexObserver(TranscriptObserver):
    """Read `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.

    Note the date directories: rollout files are filed under the day the
    session started, in UTC, which is not the local date for most of the world
    for part of every day.
    """

    #: The process holds its rollout open, so ownership can be shown rather than inferred.
    proves_ownership = True
    trajectory_capabilities = TrajectoryCapabilities(
        supported=frozenset(
            {
                TrajectoryFeature.REQUESTS,
                TrajectoryFeature.MODELS,
                TrajectoryFeature.TOOLS,
                TrajectoryFeature.USAGE,
                TrajectoryFeature.TIMING,
                TrajectoryFeature.REASONING,
                TrajectoryFeature.CONTEXT,
                TrajectoryFeature.LIVE_UPDATES,
            }
        ),
        unsupported=frozenset({TrajectoryFeature.RETRIES}),
    )

    def __init__(
        self,
        root: Path | None = None,
        pane_pid: int | None = None,
        session_exact: bool = False,
        session_provenance: str | TranscriptProvenance | None = None,
    ):
        #: Injectable so tests never touch the real ~/.codex.
        self.root = root or Path.home() / ".codex" / "sessions"
        #: The participant's launch process. Set only on the `open_source_for` clone.
        self.pane_pid = pane_pid
        self._last_model: str | None = None
        #: Whether the id this clone opened with is itself proof — token or receipt, not file-read.
        provenance = normalize_provenance(session_provenance)
        self._session_exact = session_exact or provenance is TranscriptProvenance.EXACT
        #: Rollouts held open by this clone's process; resolved so another spelling still matches.
        self._proved: set[Path] = set()

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
    ) -> Source:
        """A source that can report a process-proven location as exact.

        A clone when the caller's provenance disagrees with this instance's.
        The two are one fact seen from two sides — which key discovery asks
        first, and how the answer is labelled — and a caller that says its id
        is exact while the observer still thinks otherwise would get the
        process asked ahead of an id it told us to trust.
        """
        provenance = normalize_provenance(session_provenance)
        session_exact = provenance is TranscriptProvenance.EXACT
        reader = self
        if session_exact != self._session_exact:
            reader = CodexObserver(
                root=self.root, pane_pid=self.pane_pid, session_exact=session_exact
            )
        return _CodexSource(
            reader,
            cwd=cwd,
            session_id=session_id,
            after=after,
            session_provenance=provenance,
            known_location=known_location,
        )

    def open_source_for(
        self,
        *,
        participant_id: str,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
        pane_pid: int | None = None,
    ) -> Source:
        """Give this participant's watcher its own reader, holding its own pid.

        A clone rather than `self`, for the same reason vibe clones: the
        observer on the harness is shared by every codex session, and the pid
        — and what it has proved — is the one thing that is per-participant.
        """
        provenance = normalize_provenance(session_provenance)
        session_exact = provenance is TranscriptProvenance.EXACT
        reader = CodexObserver(root=self.root, pane_pid=pane_pid, session_exact=session_exact)
        return reader.open_source(
            cwd=cwd,
            session_id=session_id,
            after=after,
            session_provenance=provenance,
            known_location=known_location,
        )

    def proved(self, path: Path) -> bool:
        """Whether this clone's own process was found holding *path* open."""
        return _resolve(path) in self._proved

    def find_transcript(
        self,
        *,
        cwd: str,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        if not self.root.is_dir():
            return None
        if session_id and self._session_exact:
            # An id that is itself proof outranks the process; names the file, one glob.
            hit = self._by_session_id(session_id)
            if hit is not None:
                return hit
        held = self.proven_transcript(cwd=cwd)
        if held is not None:
            return held
        if session_id:
            # Only an unsure id reaches here — read from a file an earlier guess picked.
            hit = self._by_session_id(session_id)
            if hit is not None:
                return hit
        return self._scan_by_cwd(cwd, after)

    def _by_session_id(self, session_id: str) -> Path | None:
        """The rollout whose filename carries *session_id*.

        The uuid suffix of the filename is the session id, so this is an exact
        lookup: no scan, and no need to guess the date directory.
        """
        return next(self.root.glob(f"*/*/*/rollout-*-{session_id}.jsonl"), None)

    def proven_transcript(self, *, cwd: str | None) -> Path | None:
        """The rollout this participant's own process is holding open, if any.

        Discovery's proof half, callable on its own. A source that already has
        an admitted location needs to ask for proof *without* asking for a
        guess: `find_transcript` would fall through to the cwd scan, and
        letting a scan replace an admitted location is the drift the whole
        collision guard exists to prevent.
        """
        held = self._process_rollout(cwd)
        if held is not None:
            self._proved.add(held)
        return held

    def _scan_by_cwd(self, cwd: str, after: float | None) -> Path | None:
        """The oldest channel: newest rollout whose `session_meta` cwd matches.

        Kept exactly as it was, and reached only once the sharper keys have
        had their turn. On its own it cannot tell two siblings apart, which is
        the whole reason the process probe above it exists.
        """
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
                # stat, not the filename: its timestamp is local, no offset; caller floor is epoch.
                born = getattr(st, "st_birthtime", st.st_ctime)
                if born < after:
                    continue
            candidates.append((st.st_mtime, path))
        # Collect all matches so an ambiguity is logged, not silent: two siblings in the same cwd.
        matches: list[Path] = []
        for _, path in sorted(candidates, reverse=True):
            if self._transcript_cwd(path) == want:
                matches.append(path)
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "codex find_transcript: %d transcripts match cwd %s; "
                "returning the newest — the observer will refuse a collision",
                len(matches),
                cwd,
            )
        return matches[0]

    def transcript_candidates(
        self,
        *,
        cwd: str | None,
        domain: str | None = None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        root = Path(domain).resolve() if domain else self.root.resolve()
        if not root.is_dir():
            return []
        want = str(Path(cwd).resolve()) if cwd else None
        resolved_domain = str(root)
        rows = [
            self._candidate_row(path, want=want, after=after, domain=resolved_domain)
            for path in root.glob("*/*/*/rollout-*.jsonl")
        ]
        return sorted(rows, key=lambda c: (c.mtime or 0, c.location), reverse=True)

    def identity_loss_candidate(
        self,
        *,
        cwd: str | None,
        current: Path,
        current_mtime_ns: int,
        after: float | None = None,
    ) -> Path | None:
        """A newer cwd match, bounded and never itself process proof."""
        if not self.root.is_dir() or not cwd:
            return None
        want = str(Path(cwd).resolve())
        candidates: list[tuple[int, Path]] = []
        for path in self.root.glob("*/*/*/rollout-*.jsonl"):
            if path == current or path.is_symlink():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime_ns <= current_mtime_ns:
                continue
            if after is not None and getattr(st, "st_birthtime", st.st_ctime) < after:
                continue
            candidates.append((st.st_mtime_ns, path))
        for _mtime, path in sorted(candidates, reverse=True)[:_LOSS_CANDIDATE_PROBES]:
            if self._transcript_cwd(path) == want:
                return path
        return None

    def admit_operator_candidate(
        self,
        *,
        cwd: str | None,
        candidate: str,
        domain: str | None = None,
        after: float | None = None,
    ) -> TranscriptCandidate:
        want = str(Path(cwd).resolve()) if cwd else None
        root = Path(domain).resolve() if domain else self.root.resolve()
        path = Path(candidate).expanduser()
        if path.is_symlink():
            raise ValueError("candidate path is a symlink")
        real = path.resolve()
        if not real.is_relative_to(root):
            raise ValueError("candidate path is outside this harness transcript domain")
        row = self._candidate_row(real, want=want, after=after, domain=str(root))
        if row.rejection_reason:
            raise ValueError(row.rejection_reason)
        return row

    def _candidate_row(
        self,
        path: Path,
        *,
        want: str | None,
        after: float | None,
        domain: str,
    ) -> TranscriptCandidate:
        reason = None
        session_id = self.session_id(path)
        try:
            st = path.stat()
        except OSError:
            return TranscriptCandidate(
                location=str(path), rejection_reason="not readable", domain=domain
            )
        if after is not None and getattr(st, "st_birthtime", st.st_ctime) < after:
            reason = "created before participant floor"
        elif not self._is_rollout_shape(path, root=Path(domain)):
            reason = "harness shape mismatch"
        elif session_id is None:
            reason = "unextractable session id"
        else:
            found_cwd = self._transcript_cwd(path)
            if found_cwd is None:
                reason = "harness mismatch or unextractable cwd"
            elif want is not None and found_cwd != want:
                reason = "cwd mismatch"
        return TranscriptCandidate(
            location=str(path),
            session_id=session_id,
            mtime=st.st_mtime,
            size=st.st_size,
            rejection_reason=reason,
            domain=domain,
        )

    def _is_rollout_shape(self, path: Path, *, root: Path | None = None) -> bool:
        if path.suffix != ".jsonl" or _STEM.match(path.stem) is None:
            return False
        try:
            relative = path.resolve().relative_to((root or self.root).resolve())
        except (OSError, ValueError):
            return False
        return len(relative.parts) == 4

    def _process_rollout(self, cwd: str | None) -> Path | None:
        """The rollout this participant's own codex process holds open.

        Four conditions, all required, and the last is the one that matters:
        the file is under the configured transcript root, it is named like a
        rollout, its `session_meta` records the participant's working
        directory, and **the one process that speaks for this participant**
        holds exactly one such file open. Two would mean we do not understand
        what we are looking at, and guessing between them is the
        mis-attribution this whole path exists to prevent — so that answers
        `None` and lets the cwd scan and the reducer's guard handle it as
        before.

        Note that the process is chosen before its files are read, rather than
        pooling the open files of every codex in the pane. Pooling makes the
        count of *rollouts* stand in for the count of *possible owners*, and
        the two differ exactly when it is dangerous: two codex processes where
        only one has written its rollout yet pool to a single file, which then
        looks like proof and can be the other one's.

        The birth-time floor is deliberately not applied. It is a proxy for
        ownership, and we are holding the thing it was a proxy for; a resumed
        session whose rollout predates the participant is still that
        participant's rollout.
        """
        pid = self._owning_process()
        if pid is None:
            return None
        want = _resolve(Path(cwd)) if cwd else None
        root = _resolve(self.root)
        found: set[Path] = set()
        for path in proc.open_files(pid):
            if not self._is_rollout(path, root):
                continue
            if want is not None and self._transcript_cwd(path) != str(want):
                continue
            found.add(_resolve(path))
        if not found:
            return None
        if len(found) > 1:
            logger.warning(
                "codex process %s holds %d rollouts open under %s; "
                "declining to pick one — falling back to cwd discovery",
                pid,
                len(found),
                self.root,
            )
            return None
        return found.pop()

    def _owning_process(self) -> int | None:
        """The pane's own process, and only if that process is codex itself.

        A pane Theater spawned runs codex as the pane process, so `pane_pid`
        *is* the CLI. That identity is durable: the registry recorded the pid
        of the process it started, and while the participant lives that pid
        names that session. Anything codex spawned below it — the agent's
        tooling, or a codex it launched as a sub-agent — belongs to a
        different session, so descendants are not consulted at all.

        A pane whose root is something else, a shell for an adopted session,
        gets no answer here. Searching beneath it is what the obvious version
        of this does, and it is wrong in a way counting cannot fix: a shell
        outlives the CLI it ran. Find exactly one codex under an adopted pane
        and you have learned that one codex is running there *now*, not that
        it is the one the participant was adopted from — the operator can have
        quit the first and started a second, and the second's rollout would
        then be proved as the first's. Uniqueness is not identity.

        Closing that properly needs a durable association between the
        participant and the process, established when the pane was adopted and
        owned by the daemon, which is the only thing that may hold such state.
        Until then this fails closed: no proof for adopted panes, which leaves
        them exactly where they were — the cwd scan, and a collision guard that
        refuses what it cannot tell apart.
        """
        if self.pane_pid is None:
            return None
        if not _is_codex(proc.comm(self.pane_pid)):
            return None
        return self.pane_pid

    def _is_rollout(self, path: Path, root: Path) -> bool:
        """Under the configured root, and named the way codex names a rollout."""
        if path.suffix != ".jsonl" or _STEM.match(path.stem) is None:
            return False
        return _resolve(path).is_relative_to(root)

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
        record = self._decode(line)
        if record is None:
            return []
        return self._parse_decoded(record, index, clip_text=clip_text)

    @staticmethod
    def _decode(line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None
        try:
            record = json.loads(line)
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        return record

    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        record = self._decode(line)
        if record is None:
            return ParsedRecord()
        events = tuple(self._parse_decoded(record, index, clip_text=clip_text))
        payload = record.get("payload")
        redundant = (
            record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") in {"user_message", "agent_message"}
        )
        return ParsedRecord(
            events=events,
            trajectory=tuple(self._trajectory_facts(record, index)),
            trajectory_events=() if redundant else None,
        )

    def _parse_decoded(self, record: dict, index: int, *, clip_text: bool = True) -> list[Event]:
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
            raw = payload.get("message") if isinstance(payload.get("message"), str) else ""
            return [
                Event(
                    kind=EventKind.USER,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    raw_index=index,
                )
            ]
        if ptype == "agent_message":
            if payload.get("phase") == "final_answer":
                # Repeated by the task_complete that follows; emitting both doubles each reply.
                return []
            raw = payload.get("message") if isinstance(payload.get("message"), str) else ""
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    raw_index=index,
                )
            ]
        if ptype == "task_complete":
            raw = (
                payload.get("last_agent_message")
                if isinstance(payload.get("last_agent_message"), str)
                else ""
            )
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    turn_end=True,
                    turn_id=_turn_id(payload),
                    raw_index=index,
                )
            ]
        if ptype == "turn_aborted":
            raw = f"turn aborted: {payload.get('reason') or 'unknown'}"
            return [
                Event(
                    kind=EventKind.ERROR,
                    text=raw,
                    raw_text=raw,
                    ts=ts,
                    turn_end=True,
                    turn_id=_turn_id(payload),
                    raw_index=index,
                )
            ]
        if ptype in ("mcp_tool_call_begin", "mcp_tool_call_end"):
            # Only visibility into MCP use, Theater tools included: never in response_items.
            invocation = payload.get("invocation")
            invocation = invocation if isinstance(invocation, dict) else {}
            tool_name = ".".join(
                str(part) for part in (invocation.get("server"), invocation.get("tool")) if part
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
            raw = self._mcp_result(payload.get("result"))
            return [
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=_clip(raw),
                    raw_text=raw,
                    tool_name=tool_name or None,
                    ts=ts,
                    raw_index=index,
                )
            ]
        if ptype == "token_count":
            return self._token_count(payload, ts, index)
        if ptype == "thread_settings_applied":
            settings = payload.get("thread_settings")
            if isinstance(settings, dict):
                m = settings.get("model")
                if isinstance(m, str) and m:
                    self._last_model = m
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

    def _token_count(self, payload: dict, ts: float | None, index: int) -> list[Event]:
        """Extract per-turn usage from a token_count event_msg."""
        info = payload.get("info")
        if not isinstance(info, dict):
            return []
        last = info.get("last_token_usage")
        if not isinstance(last, dict):
            return []
        total = info.get("total_token_usage") or {}
        if not isinstance(total, dict):
            return []
        fields = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
        totals = tuple(int(total.get(field) or 0) for field in fields)
        latest = tuple(int(last.get(field) or 0) for field in fields)
        model = info.get("model") or info.get("model_name") or self._last_model
        model = model or None if isinstance(model, str) else None
        input_tokens, cache_read, cache_write, output_tokens, reasoning = latest
        usage = TokenUsage(
            model=model,
            input_tokens=max(0, input_tokens - cache_read - cache_write),
            output_tokens=max(0, output_tokens - reasoning),
            cache_creation_input_tokens=cache_write,
            cache_read_input_tokens=cache_read,
            reasoning_output_tokens=reasoning,
            idempotency_key="codex:" + ":".join(str(value) for value in totals + latest),
        )
        if (
            usage.input_tokens == 0
            and usage.output_tokens == 0
            and usage.cache_creation_input_tokens == 0
            and usage.cache_read_input_tokens == 0
            and usage.reasoning_output_tokens == 0
        ):
            return []
        return [Event(kind=EventKind.ASSISTANT, ts=ts, raw_index=index, usage=usage)]

    def _item(self, payload: dict, ts: float | None, index: int, *, clip_text: bool) -> list[Event]:
        _clip = clipper(clip_text)
        ptype = payload.get("type")

        if ptype in ("custom_tool_call", "function_call"):
            name = payload.get("name")
            paths: tuple[EventPath, ...] = ()
            if name == "apply_patch":
                # Patch markers are structured, not prose — path extraction reads a field.
                raw_input = payload.get("input")
                paths = _apply_patch_paths(raw_input if isinstance(raw_input, str) else "")
            return [
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=name,
                    ts=ts,
                    raw_index=index,
                    paths=paths,
                )
            ]
        if ptype in ("custom_tool_call_output", "function_call_output"):
            # No tool name: record carries only `call_id`; resolving needs state across lines.
            raw = _flatten(payload.get("output"))
            return [
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    raw_index=index,
                )
            ]
        # `message` duplicates event_msg and `reasoning` is private thinking; both dropped.
        return []

    def _trajectory_facts(  # noqa: PLR0912, PLR0915
        self, record: dict, index: int
    ) -> list[TrajectoryFact]:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return []
        timestamp = _epoch(record.get("timestamp"))
        timing = _codex_timing(record, payload, timestamp)
        record_id = _trajectory_id(record.get("id") or record.get("uuid"))
        turn_id = _codex_trajectory_turn_id(payload)
        step_id = _trajectory_id(payload.get("step_id") or payload.get("stepId"))
        facts: list[TrajectoryFact] = []

        def add(
            kind: TrajectoryKind,
            lane: TrajectoryLane,
            summary: str = "",
            *,
            native_id: str | None = None,
            status: TrajectoryStatus = TrajectoryStatus.UNKNOWN,
            turn: str | None = turn_id,
            step: str | None = step_id,
            call_id: str | None = None,
            parent_call_id: str | None = None,
            fact_timing: Timing | None = timing,
            usage: TrajectoryUsage | None = None,
            details: tuple[DetailField, ...] = (),
        ) -> None:
            clean_id = _trajectory_id(native_id)
            facts.append(
                TrajectoryFact(
                    kind=kind,
                    lane=lane,
                    source="codex",
                    summary=_safe_trajectory_text(summary),
                    status=status,
                    native_id=clean_id,
                    revision=_codex_revision(record, payload),
                    raw_index=index,
                    event_ordinal=len(facts),
                    turn_id=turn,
                    step_id=step,
                    call_id=_trajectory_id(call_id),
                    parent_call_id=_trajectory_id(parent_call_id),
                    timing=fact_timing,
                    usage=usage,
                    details=details,
                )
            )

        record_kind = record.get("type")
        ptype = payload.get("type")
        if record_kind == "session_meta":
            session_id = _trajectory_id(payload.get("session_id") or payload.get("id"))
            add(
                TrajectoryKind.SYSTEM,
                TrajectoryLane.MODEL,
                "session metadata",
                native_id=session_id or record_id,
                status=_trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED),
                details=(_trajectory_detail("session", payload, format=ContentFormat.JSON),),
            )
            return facts

        if record_kind in ("turn_context", "thread_settings_applied", "world_state"):
            context = payload.get("thread_context") or payload.get("thread_settings")
            context = context if context is not None else payload.get("state")
            if context is None:
                context = payload
            context_id = _trajectory_id(payload.get("id")) or record_id
            if context_id is None:
                context_id = _codex_scoped_id(turn_id, str(record_kind))
            model = None
            if isinstance(context, dict):
                model = _trajectory_id(context.get("model") or context.get("model_name"))
            summary = (
                "turn context"
                if record_kind == "turn_context"
                else str(record_kind).replace("_", " ")
            )
            if model:
                summary = f"{summary}: {model}"
            add(
                TrajectoryKind.CONTEXT,
                TrajectoryLane.MODEL,
                summary,
                native_id=context_id,
                turn=_codex_trajectory_turn_id(context) if isinstance(context, dict) else turn_id,
                status=_trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED),
                details=(_trajectory_detail("context", context, format=ContentFormat.JSON),),
            )
            return facts

        if record_kind == "event_msg":
            event_type = payload.get("type")
            event_id = _trajectory_id(
                payload.get("id") or payload.get("message_id") or payload.get("item_id")
            )
            if event_type == "user_message":
                return facts
            if event_type == "agent_message":
                return facts
            if event_type == "task_complete":
                completed_timing = _codex_timing(record, payload, timestamp)
                add(
                    TrajectoryKind.CONTEXT,
                    TrajectoryLane.MODEL,
                    "turn completed",
                    native_id=_trajectory_id(payload.get("id"))
                    or _codex_scoped_id(turn_id, "completed"),
                    status=TrajectoryStatus.COMPLETED,
                    turn=_turn_id(payload),
                    fact_timing=completed_timing,
                    details=(
                        _trajectory_detail(
                            "output",
                            payload.get("last_agent_message"),
                            format=ContentFormat.TEXT,
                        ),
                    ),
                )
                return facts
            if event_type == "turn_aborted":
                reason = payload.get("reason") or "unknown"
                add(
                    TrajectoryKind.ERROR,
                    TrajectoryLane.THEATER,
                    f"turn aborted: {_safe_trajectory_text(reason)}",
                    native_id=event_id or _codex_scoped_id(turn_id, "aborted"),
                    status=TrajectoryStatus.INTERRUPTED,
                    turn=_turn_id(payload),
                )
                return facts
            if event_type in ("mcp_tool_call_begin", "mcp_tool_call_end"):
                invocation = payload.get("invocation")
                invocation = invocation if isinstance(invocation, dict) else {}
                mcp_parts = [invocation.get("server"), invocation.get("tool")]
                tool_name = ".".join(str(part) for part in mcp_parts if part)
                call_id = _trajectory_id(payload.get("call_id"))
                if event_type == "mcp_tool_call_begin":
                    args = invocation.get("arguments") or invocation.get("input")
                    mcp_details = (
                        (_trajectory_detail("input", args, format=ContentFormat.JSON),)
                        if args is not None
                        else ()
                    )
                    add(
                        TrajectoryKind.TOOL_CALL,
                        TrajectoryLane.TOOLS,
                        tool_name or "MCP tool call",
                        native_id=event_id or _codex_scoped_id(call_id, "call"),
                        status=_trajectory_status(payload.get("status"), TrajectoryStatus.PENDING),
                        call_id=call_id,
                        parent_call_id=_trajectory_id(
                            payload.get("parent_call_id") or payload.get("parent_id")
                        ),
                        details=mcp_details,
                    )
                else:
                    result = payload.get("result")
                    raw = self._mcp_result(result)
                    result_error = isinstance(result, dict) and result.get("Err") is not None
                    if isinstance(result, dict):
                        ok = result.get("Ok")
                        if isinstance(ok, dict):
                            result_error = result_error or ok.get("isError") is True
                    add(
                        TrajectoryKind.TOOL_RESULT,
                        TrajectoryLane.TOOLS,
                        raw,
                        native_id=event_id or _codex_scoped_id(call_id, "result"),
                        status=TrajectoryStatus.ERROR
                        if result_error
                        else _trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED),
                        call_id=call_id,
                        parent_call_id=_trajectory_id(
                            payload.get("parent_call_id") or payload.get("parent_id")
                        )
                        or call_id,
                        details=(
                            (_trajectory_detail("result", result, format=ContentFormat.JSON),)
                            if result is not None
                            else ()
                        ),
                    )
                return facts
            if event_type == "token_count":
                usage = _codex_usage(record, payload)
                if usage is not None:
                    add(
                        TrajectoryKind.ASSISTANT,
                        TrajectoryLane.MODEL,
                        "token usage",
                        native_id=event_id,
                        status=TrajectoryStatus.COMPLETED,
                        usage=usage,
                    )
                return facts
            if event_type in (
                "task_started",
                "context_compacted",
                "turn_context",
                "thread_settings_applied",
            ):
                status = (
                    TrajectoryStatus.RUNNING
                    if event_type == "task_started"
                    else _trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED)
                )
                add(
                    TrajectoryKind.CONTEXT,
                    TrajectoryLane.MODEL,
                    event_type.replace("_", " "),
                    native_id=event_id or _codex_scoped_id(turn_id, event_type),
                    status=status,
                    turn=_turn_id(payload),
                    details=(
                        (_trajectory_detail("payload", payload, format=ContentFormat.JSON),)
                        if payload
                        else ()
                    ),
                )
                return facts
            return facts

        if record_kind == "response_item":
            item_type = payload.get("type")
            item_id = _trajectory_id(payload.get("id"))
            item_turn = _codex_trajectory_turn_id(payload) or turn_id
            if item_type == "message":
                role = payload.get("role")
                content = payload.get("content")
                blocks = content if isinstance(content, list) else []
                if isinstance(content, str):
                    blocks = [{"type": "text", "text": content}]
                message_kind = (
                    TrajectoryKind.USER
                    if role == "user"
                    else TrajectoryKind.SYSTEM
                    if role in {"system", "developer"}
                    else TrajectoryKind.ASSISTANT
                )
                lane = (
                    TrajectoryLane.INPUT
                    if message_kind is TrajectoryKind.USER
                    else TrajectoryLane.MODEL
                )
                message_status = _trajectory_status(
                    payload.get("status"), TrajectoryStatus.COMPLETED
                )
                for block_index, block in enumerate(blocks):
                    if not isinstance(block, dict):
                        continue
                    block_text = block.get("text")
                    if not isinstance(block_text, str):
                        continue
                    add(
                        message_kind,
                        lane,
                        block_text,
                        native_id=_codex_block_id(item_id, block, block_index),
                        status=message_status,
                        turn=item_turn,
                        usage=_codex_usage(record, payload),
                    )
                if not facts:
                    add(
                        message_kind,
                        lane,
                        _codex_content_text(content),
                        native_id=item_id,
                        status=message_status,
                        turn=item_turn,
                        usage=_codex_usage(record, payload),
                    )
                return facts
            if item_type == "reasoning":
                reasoning_parts: list[tuple[str, dict]] = []
                reasoning_summary = payload.get("summary")
                if isinstance(reasoning_summary, str):
                    reasoning_parts.append(
                        (reasoning_summary, {"type": "summary_text", "text": reasoning_summary})
                    )
                elif isinstance(reasoning_summary, list):
                    for block in reasoning_summary:
                        if not isinstance(block, dict):
                            continue
                        block_text = block.get("text")
                        if isinstance(block_text, str):
                            reasoning_parts.append((block_text, block))
                content = payload.get("content")
                if isinstance(content, str):
                    reasoning_parts.append((content, {"type": "content", "text": content}))
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_text = block.get("text")
                        if isinstance(block_text, str):
                            reasoning_parts.append((block_text, block))
                for part_index, (text, block) in enumerate(reasoning_parts):
                    add(
                        TrajectoryKind.REASONING,
                        TrajectoryLane.MODEL,
                        _safe_trajectory_text(text),
                        status=_trajectory_status(
                            payload.get("status"), TrajectoryStatus.COMPLETED
                        ),
                        native_id=_codex_block_id(item_id, block, part_index),
                        turn=item_turn,
                        details=(
                            _trajectory_detail("reasoning", block, format=ContentFormat.JSON),
                        ),
                    )
                return facts
            call_id = _trajectory_id(payload.get("call_id"))
            parent_call_id = _trajectory_id(
                payload.get("parent_call_id") or payload.get("parent_id")
            )
            call_types = {
                "custom_tool_call",
                "function_call",
                "local_shell_call",
                "web_search_call",
                "computer_call",
                "mcp_tool_call",
            }
            result_types = {
                "custom_tool_call_output",
                "function_call_output",
                "local_shell_call_output",
                "web_search_call_output",
                "computer_call_output",
                "mcp_tool_call_output",
            }
            if item_type in call_types:
                name = _safe_trajectory_text(payload.get("name") or item_type)
                input_value = payload.get("input")
                if input_value is None:
                    input_value = payload.get("arguments")
                add(
                    TrajectoryKind.TOOL_CALL,
                    TrajectoryLane.TOOLS,
                    name,
                    native_id=item_id or call_id,
                    status=_trajectory_status(payload.get("status"), TrajectoryStatus.PENDING),
                    turn=item_turn,
                    call_id=call_id,
                    parent_call_id=parent_call_id,
                    usage=_codex_usage(record, payload),
                    details=(
                        (_trajectory_detail("input", input_value, format=ContentFormat.JSON),)
                        if input_value is not None
                        else ()
                    ),
                )
            elif item_type in result_types:
                output = payload.get("output")
                if output is None:
                    output = payload.get("result")
                output_text = _codex_content_text(output)
                add(
                    TrajectoryKind.TOOL_RESULT,
                    TrajectoryLane.TOOLS,
                    output_text,
                    native_id=item_id,
                    status=_trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED),
                    turn=item_turn,
                    call_id=call_id,
                    parent_call_id=parent_call_id or call_id,
                    details=(
                        (
                            _trajectory_detail(
                                "result",
                                output,
                                format=(
                                    ContentFormat.TEXT
                                    if isinstance(output, str)
                                    else ContentFormat.JSON
                                ),
                            ),
                        )
                        if output is not None
                        else ()
                    ),
                )
            return facts

        if record_kind in ("system", "context", "compaction") or ptype in (
            "system",
            "context",
            "compaction",
        ):
            body = payload.get("message") or payload.get("content") or payload.get("summary")
            system_details = (
                (_trajectory_detail("payload", payload, format=ContentFormat.JSON),)
                if payload
                else ()
            )
            add(
                TrajectoryKind.CONTEXT if ptype != "system" else TrajectoryKind.SYSTEM,
                TrajectoryLane.MODEL,
                _codex_content_text(body) or str(record_kind or ptype).replace("_", " "),
                native_id=record_id or _trajectory_id(payload.get("id")),
                status=_trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED),
                details=system_details,
            )
        return facts

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

        The first-launch trust dialog also trips this boolean, because it
        renders a `›` selection row just like the idle composer. That is why
        `screen_reading` must check the TRUST and APPROVAL markers before
        falling through to this method: without that guard both modals would
        classify as PROMPT and the send gate would inject into them.
        """
        if WORKING_MARKER in capture:
            return False
        lines = [line.strip() for line in capture.splitlines() if line.strip()]
        return any(line.startswith(PROMPT) for line in lines[-_SCREEN_TAIL_LINES:])

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify the rendered screen as trust, approval, working, or prompt.

        Arm order is load-bearing: both the trust dialog and the approval
        overlay render a selection row starting with `›`, so
        `is_idle_screen` returns True on both. The modal arms must therefore
        come before the `is_idle_screen` call, or both modals would classify
        as PROMPT and the send gate would inject into a live approval.
        """
        if TRUST_MARKER in capture:
            return ScreenReading(kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH)
        if _in_screen_tail(capture, APPROVAL_MARKER):
            return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
        if WORKING_MARKER in capture:
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        if self.is_idle_screen(capture):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)


#: What the loader looks for. An instance, not the class (see docs/harness-plugins.md).
HARNESS = CodexHarness()
