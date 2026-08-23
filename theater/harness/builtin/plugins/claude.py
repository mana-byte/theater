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
import logging
import math
import shlex
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from theater import paths
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
    last_screen_line,
    theater_binary,
)
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.harness.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
    TranscriptObserver,
)
from theater.harness.source import Batch, ReceiptAdmission, TranscriptCandidate, TranscriptSource
from theater.models import BadRequest
from theater.provenance import TranscriptProvenance
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

logger = logging.getLogger("theater.harness.claude")
CLAUDE_RECEIPT_COMMAND = "claude-receipt"
CLAUDE_RECEIPT_EVENTS = ("SessionStart", "PreCompact")

#: Screen lines that mean "waiting for you".
IDLE_PROMPTS = (">", "> ")

#: Footer of every approval dialog — chosen over the question text (can appear in echoed output).
APPROVAL_MARKER = "Esc to cancel"

#: Workspace-trust onboarding dialog's primary option label, unique to that dialog.
TRUST_MARKER = "Yes, I trust this folder"

#: Status footer segment while a turn is in flight; mutually exclusive with IDLE_FOOTER.
WORKING_MARKER = "esc to interrupt"

#: Status footer segment while waiting for input. Not the `manual mode on` indicator.
IDLE_FOOTER = "? for shortcuts"

#: Alternate idle footer shown when the agent switcher occupies the shortcut slot.
IDLE_AGENTS_FOOTER = "← for agents"
#: Claude renders a different leading glyph for manual and accept-edits modes; both are chrome.
MODE_LINE_PREFIXES = ("⏸", "⏵⏵")

#: How far up from the bottom to look for the prompt and footer.
_SCREEN_TAIL_LINES = 6

#: Records to read before giving up on finding a `cwd` in a candidate transcript.
_CWD_PROBE_RECORDS = 20

#: Bound the probe read so scanning candidates never turns into reading whole transcripts.
_CWD_PROBE_BYTES = 256 * 1024

#: Only this many newest files have their records opened during a loss probe.
_LOSS_CANDIDATE_PROBES = 8


#: Tools that write a file, keyed by the input parameter carrying the path.
_WRITE_TOOLS: dict[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

#: Tools that read a file. Grep and Glob take a directory or pattern, not a named file.
_READ_TOOLS: dict[str, str] = {
    "Read": "file_path",
}


def _claude_settings_path(participant_id: str) -> Path:
    """Launch-specific Claude settings for receipt hooks.

    Lives under the shared Claude settings namespace, not the per-participant
    observation dir, because ``--settings`` is a single file the CLI reads at
    startup and its hooks reference the token path inside it.
    """
    return paths.home() / "claude" / f"{participant_id}.settings.json"


def _receipt_hook_command(participant_id: str, token_path: Path) -> str:
    """Command run by Claude lifecycle hooks.

    Claude's hook schema accepts command-type hooks under ``hooks.<event>[]``.
    The hook process receives the lifecycle JSON on stdin; the command keeps
    Theater's token out of argv by passing only the private token-file path.
    """
    return shlex.join(
        [
            theater_binary(),
            CLAUDE_RECEIPT_COMMAND,
            "--id",
            participant_id,
            "--token-file",
            str(token_path),
        ]
    )


def _hook_string(data: Mapping[str, object], *names: str) -> str | None:
    """Extract the first non-empty string value for any of ``names``."""
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _claude_receipt_settings(participant_id: str, token_path: Path) -> dict:
    """Launch-local Claude settings layered via ``--settings``.

    User settings remain untouched. ``SessionStart`` covers cold starts,
    resumes, clears and forks. ``PreCompact`` records the old location before a
    compaction boundary; the post-compaction ``SessionStart`` supplies the new
    one when Claude rotates. ``Stop`` is intentionally absent because it does
    not prove a new transcript location.
    """
    hook = {"type": "command", "command": _receipt_hook_command(participant_id, token_path)}
    entry = {"hooks": [hook]}
    return {"hooks": {event: [entry] for event in CLAUDE_RECEIPT_EVENTS}}


def _epoch(value) -> float | None:
    """Claude Code writes ISO-8601 with a Z suffix."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _token_usage(message: dict, record: dict) -> TokenUsage | None:
    """Extract usage from a Claude assistant record."""
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return None
    model = message.get("model")
    if not isinstance(model, str) or not model:
        model = None
    cost = record.get("costUSD")
    cost = float(cost) if isinstance(cost, (int, float)) and cost > 0 else None
    native_id = message.get("id") or record.get("requestId")
    usage_key = f"claude:{native_id}" if isinstance(native_id, str) and native_id else None
    return TokenUsage(
        model=model,
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_creation_input_tokens=int(raw.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(raw.get("cache_read_input_tokens") or 0),
        cost_usd=cost,
        idempotency_key=usage_key,
    )


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
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        return None
    if len(value.encode("utf-8")) > TRAJECTORY_IDENTIFIER_MAX_BYTES:
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


def _trajectory_duration(record: dict) -> float | None:
    for key in ("durationMs", "duration_ms"):
        value = _trajectory_float(record.get(key))
        if value is not None and value >= 0:
            return value
    return None


def _claude_timing(record: dict, timestamp: float | None) -> Timing | None:
    start = next(
        (
            _trajectory_time(record.get(key))
            for key in ("startTimestamp", "start_timestamp", "startedAt", "started_at")
            if _trajectory_time(record.get(key)) is not None
        ),
        None,
    )
    end = next(
        (
            _trajectory_time(record.get(key))
            for key in ("endTimestamp", "end_timestamp", "completedAt", "completed_at")
            if _trajectory_time(record.get(key)) is not None
        ),
        None,
    )
    if start is None and timestamp is not None:
        start = timestamp
    duration = _trajectory_duration(record)
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
    normalized = value.lower().replace("-", "_")
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
        "in_progress": TrajectoryStatus.RUNNING,
        "running": TrajectoryStatus.RUNNING,
        "partial": TrajectoryStatus.PARTIAL,
        "interrupted": TrajectoryStatus.INTERRUPTED,
        "pending": TrajectoryStatus.PENDING,
    }
    return aliases.get(normalized, default)


def _claude_trajectory_usage(message: dict, record: dict) -> TrajectoryUsage | None:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return None
    model = _trajectory_id(message.get("model"))
    request_id = _trajectory_id(
        record.get("requestId") or message.get("request_id") or message.get("id")
    )
    cost = _trajectory_float(record.get("costUSD"))
    return TrajectoryUsage(
        model=model,
        request_id=request_id,
        input_tokens=_trajectory_int(raw.get("input_tokens")),
        output_tokens=_trajectory_int(raw.get("output_tokens")),
        reasoning_tokens=_trajectory_int(raw.get("reasoning_output_tokens")),
        cache_read_tokens=_trajectory_int(raw.get("cache_read_input_tokens")),
        cache_write_tokens=_trajectory_int(raw.get("cache_creation_input_tokens")),
        cost_usd=cost if cost is None or cost >= 0 else None,
    )


def _claude_revision(record: dict) -> int:
    message = record.get("message")
    values = [record, message] if isinstance(message, dict) else [record]
    for value in values:
        for key in ("revision", "version"):
            candidate = _trajectory_int(value.get(key))
            if candidate or value.get(key) in (0, 0.0):
                return candidate
    return 0


def _claude_block_native_id(
    block: dict, base_id: str | None, record_id: str | None, ordinal: int
) -> str | None:
    explicit = _trajectory_id(block.get("id"))
    if explicit is not None:
        return explicit
    if base_id is not None:
        return base_id if ordinal == 0 else f"{base_id}:block:{ordinal}"
    if record_id is not None:
        return record_id if ordinal == 0 else f"{record_id}:block:{ordinal}"
    return None


def _claude_content_text(value: object) -> str:
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
    # os.path.relpath walks up with ``..`` outside the cwd — valid relative, but not a repo file.
    c = cwd.rstrip("/") + "/"
    if not (path == cwd or path.startswith(c)):
        return None
    if path == cwd:
        return "."
    return path[len(c) :]


class ClaudeCodeHarness(Harness):
    name = "claude"
    binary = "claude"
    #: Wrapper-renamed spellings for the unmanaged-pane sweep.
    binaries = frozenset({".claude-wrapped", "claude-wrapped"})
    #: Claude Code prints this spoked asterisk as its own spinner glyph.
    icon = "✻"
    #: Registration aliases; a non-normalizing spelling is observed as nothing.
    aliases = ("claude_code", "claude-code", "Claude", "ClaudeCode")

    def __init__(self, root: Path | None = None):
        #: `root` locates the transcript; nothing about launching depends on it.
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
        reasoning_effort: str | None = None,
        resume: str | None = None,
    ) -> LaunchPlan:
        if approval not in APPROVALS:
            raise BadRequest(f"approval must be one of {', '.join(APPROVALS)}, got {approval!r}")
        config = {
            "mcpServers": {
                SERVER_NAME: {
                    "command": theater_binary(),
                    "args": ["mcp", "--id", participant_id],
                }
            }
        }
        settings_path = _claude_settings_path(participant_id)
        token_path = paths.observation_dir("claude", participant_id) / "receipt-token"
        # `=` form: `--mcp-config` is variadic in 2.x — space form greedily consumes the prompt.
        argv = ["claude", f"--mcp-config={config_path}", f"--settings={settings_path}"]
        # Choosing the UUID before the pane exists removes the same-cwd creation race entirely.
        native_session_id = resume or str(uuid.uuid4())
        if resume is None:
            argv.append(f"--session-id={native_session_id}")
        if model:
            # `=` form, same as --mcp-config: space-separated value sits next to the prompt.
            argv.append(f"--model={model}")
        if reasoning_effort:
            argv.append(f"--effort={reasoning_effort}")
        if resume:
            # `--resume <id>` resumes a session; interactive reattaches and still accepts a prompt.
            argv.append(f"--resume={resume}")
        if approval == "yolo":
            argv.append("--dangerously-skip-permissions")
        elif approval == "edits":
            argv += ["--permission-mode", "acceptEdits"]
        if prompt:
            argv.append(prompt)
        return LaunchPlan(
            argv=argv,
            files={
                config_path: json.dumps(config, indent=2) + "\n",
                settings_path: json.dumps(
                    _claude_receipt_settings(participant_id, token_path),
                    indent=2,
                )
                + "\n",
            },
            private_files={},
            session_id=native_session_id,
            receipt_token_path=token_path,
        )

    def resume_launch_overlay(
        self,
        *,
        predecessor: Participant,
        trusted_session_owners: Sequence[Participant],
    ) -> ResumeLaunchOverlay:
        """Validate a predecessor's transcript domain against the observer root.

        Conditional: a predecessor with no domain is the normal case for Claude
        and returns an empty overlay. A predecessor with a domain is a new
        explicit constraint — Claude does not enforce this at bind time, so
        this is a new check, not a reuse of an existing one.
        """
        if predecessor.transcript_domain is None:
            return ResumeLaunchOverlay()
        root = self.observer.root.resolve()  # type: ignore[attr-defined]
        declared = Path(predecessor.transcript_domain).resolve(strict=False)
        if declared != root:
            raise BadRequest(
                f"cannot resume Claude session: predecessor transcript domain "
                f"{declared!r} does not match the Claude observation root {root!r}"
            )
        return ResumeLaunchOverlay(transcript_domain=str(root))


class _ClaudeSource(TranscriptSource):
    """Keep a SessionStart receipt pending until Claude creates its JSONL.

    Claude announces the exact path before the first user message materializes
    it.  A generic ``TranscriptSource`` quite reasonably treats an exact known
    location as a file that used to exist, so two absent reads mean identity
    loss.  Here the receipt is instead an expectation until one successful
    ``stat`` proves the file has existed.  From that point on the generic
    trusted-pin policy applies unchanged, including quarantine if it later
    disappears.
    """

    def __init__(self, observer: TranscriptObserver, **kwargs) -> None:
        super().__init__(observer, **kwargs)
        self._expected_location: Path | None = None

    async def read(self) -> Batch:
        self._require_decision()
        path = self._expected_location
        if path is None:
            return await super().read()
        try:
            path.stat()
        except FileNotFoundError:
            return Batch(waiting=True)
        except OSError as exc:
            return self._source_unavailable_batch(exc)

        # "expected" → "trusted": promote before attach so a racing deletion is disappearance.
        self._expected_location = None
        self._known_location = path
        self._known_location_provenance = TranscriptProvenance.EXACT
        self._proven[path] = TranscriptProvenance.EXACT
        return await super().read()

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        path = Path(location)
        self._pending = None
        self._session_id = session_id
        self._session_provenance = TranscriptProvenance.EXACT
        self._proven[path] = TranscriptProvenance.EXACT
        if self.path == path:
            self._expected_location = None
            self._known_location = path
            self._known_location_provenance = TranscriptProvenance.EXACT
            return "accepted"

        # No trusted known-location until receipt path materializes; detaching for SessionStart.
        self._expected_location = path
        self._known_location = None
        self._known_location_provenance = TranscriptProvenance.HEURISTIC
        self._detach()
        return "staged"

    def revoke_attachment(self) -> None:
        self._expected_location = None
        super().revoke_attachment()


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

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> _ClaudeSource:
        return _ClaudeSource(
            self,
            cwd=cwd,
            session_id=session_id,
            after=after,
            allow_refresh=self.relocate_by_cwd,
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
    ) -> _ClaudeSource:
        return _ClaudeSource(
            self,
            cwd=cwd,
            session_id=session_id,
            after=after,
            allow_refresh=self.relocate_by_cwd,
            session_provenance=session_provenance,
            known_location=known_location,
        )

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
            # The filename stem is the session id — no scan, no guess about the directory slug.
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
        # Collect all matches so an ambiguity is logged, not silent: two siblings in the same cwd.
        matches: list[Path] = []
        for _, path in sorted(candidates, reverse=True):
            if self._transcript_cwd(path) == want:
                matches.append(path)
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "claude find_transcript: %d transcripts match cwd %s; "
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
        rows: list[TranscriptCandidate] = []
        resolved_domain = str(root)
        for path in root.glob("*/*.jsonl"):
            rows.append(self._candidate_row(path, want=want, after=after, domain=resolved_domain))
        return sorted(rows, key=lambda c: (c.mtime or 0, c.location), reverse=True)

    def identity_loss_candidate(
        self,
        *,
        cwd: str | None,
        current: Path,
        current_mtime_ns: int,
        after: float | None = None,
    ) -> Path | None:
        """Newest bounded cwd match newer than the still-readable pin."""
        if not self.root.is_dir() or not cwd:
            return None
        want = str(Path(cwd).resolve())
        candidates: list[tuple[int, Path]] = []
        for path in self.root.glob("*/*.jsonl"):
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
        session_id = path.stem if path.suffix == ".jsonl" and path.stem else None
        try:
            st = path.stat()
        except OSError:
            return TranscriptCandidate(
                location=str(path), rejection_reason="not readable", domain=domain
            )
        if after is not None and getattr(st, "st_birthtime", st.st_ctime) < after:
            reason = "created before participant floor"
        elif path.suffix != ".jsonl" or path.parent.parent.resolve() != Path(domain).resolve():
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

    def validate_transcript_receipt(
        self,
        *,
        payload: Mapping[str, object],
        cwd: str | None,
        expected_session_id: str | None,
    ) -> TranscriptCandidate:
        """Validate a Claude lifecycle-hook receipt into a candidate.

        Extracts ``session_id``/``sessionId`` and
        ``transcript_path``/``transcriptPath`` from the opaque payload, then
        validates root containment, the ``.jsonl`` suffix, the
        ``<project>/<session>.jsonl`` filename rule, cwd, and a bounded
        20-record scan for contradicting evidence. Every failure raises
        ``ValueError`` with actionable prose; core maps it to ``BadRequest``.
        """
        session_id = _hook_string(payload, "session_id", "sessionId")
        transcript_path = _hook_string(payload, "transcript_path", "transcriptPath")
        if session_id is None:
            raise ValueError(
                "claude receipt payload is missing session_id (or sessionId); "
                "the lifecycle hook must provide it"
            )
        if transcript_path is None:
            raise ValueError(
                "claude receipt payload is missing transcript_path "
                "(or transcriptPath); the lifecycle hook must provide it"
            )
        if not isinstance(self.root, Path):
            raise ValueError(  # noqa: TRY004 – rejection is always ValueError per design
                "claude receipt cannot validate transcript root: observer root is not a Path"
            )
        root = self.root.resolve()
        path = Path(transcript_path).expanduser()
        if not path.is_absolute():
            raise ValueError(
                f"claude receipt transcript_path must be absolute, got {transcript_path!r}"
            )
        canonical = path.resolve(strict=False)
        try:
            rel = canonical.relative_to(root)
        except ValueError:
            raise ValueError(
                f"claude receipt transcript_path {transcript_path!r} is outside "
                "Claude's transcript root"
            ) from None
        if canonical.suffix != ".jsonl" or len(rel.parts) != 2:
            raise ValueError(
                "claude receipt transcript_path must name a Claude project "
                "JSONL transcript (<project>/<session>.jsonl)"
            )
        if canonical.stem != session_id:
            raise ValueError(
                f"claude receipt session_id {session_id!r} does not match "
                f"transcript_path filename stem {canonical.stem!r}"
            )
        found_evidence = self._validate_transcript_records(
            canonical, session_id=session_id, cwd=cwd
        )
        if not found_evidence and session_id != expected_session_id:
            raise ValueError(
                "claude receipt transcript does not yet contain matching "
                "evidence (no record carries session_id and the id does not "
                "match the launch session)"
            )
        return TranscriptCandidate(
            location=str(canonical),
            session_id=session_id,
        )

    @staticmethod
    def _validate_transcript_records(path: Path, *, session_id: str, cwd: str | None) -> bool:
        """Reject an existing transcript whose own records contradict the receipt."""
        if not path.exists():
            return False
        wanted_cwd = str(Path(cwd).resolve()) if cwd else None
        found_evidence = False
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i >= 20:
                        return found_evidence
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    found_session = record.get("session_id") or record.get("sessionId")
                    if (
                        isinstance(found_session, str)
                        and found_session
                        and found_session != session_id
                    ):
                        raise ValueError("claude receipt session_id contradicts transcript records")
                    if found_session == session_id:
                        found_evidence = True
                    found_cwd = record.get("cwd")
                    if (
                        wanted_cwd is not None
                        and isinstance(found_cwd, str)
                        and found_cwd
                        and str(Path(found_cwd).resolve()) != wanted_cwd
                    ):
                        raise ValueError(
                            "claude receipt transcript cwd contradicts participant cwd"
                        )
                    if isinstance(found_cwd, str) and found_cwd:
                        found_evidence = True
        except OSError as exc:
            raise ValueError(f"claude receipt transcript_path is not readable: {exc}") from exc
        return found_evidence

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
        return ParsedRecord(
            events=tuple(self._parse_decoded(record, index, clip_text=clip_text)),
            trajectory=tuple(self._trajectory_facts(record, index)),
        )

    def _parse_decoded(self, record: dict, index: int, *, clip_text: bool = True) -> list[Event]:
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
                    raw_text=text,
                    ts=ts,
                    raw_index=index,
                    # An API error ends the attempt; the agent is waiting again.
                    turn_end=True,
                )
            ]
        return []

    def _assistant(
        self,
        record: dict,
        message: dict,
        ts: float | None,
        index: int,
        *,
        clip_text: bool = True,
    ) -> list[Event]:
        _clip = clipper(clip_text)

        stop = message.get("stop_reason")
        turn_end = stop is not None and stop != "tool_use"
        tid = message.get("id") or record.get("requestId")
        tid = tid if isinstance(tid, str) and tid else None
        cwd = record.get("cwd")
        cwd = cwd if isinstance(cwd, str) and cwd else None
        usage = _token_usage(message, record)
        out: list[Event] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                raw = block.get("text") if isinstance(block.get("text"), str) else ""
                out.append(
                    Event(
                        kind=EventKind.ASSISTANT,
                        text=_clip(raw),
                        raw_text=raw,
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
        if usage is not None and out:
            out[-1] = replace(out[-1], usage=usage)
        elif usage is not None and not out and not turn_end:
            out.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    ts=ts,
                    turn_id=tid,
                    raw_index=index,
                    usage=usage,
                )
            )
        if turn_end:
            if out:
                out[-1] = replace(out[-1], turn_end=True)
            else:
                out.append(
                    Event(
                        kind=EventKind.ASSISTANT,
                        ts=ts,
                        turn_end=True,
                        turn_id=tid,
                        raw_index=index,
                        usage=usage,
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
        mode: Literal["read", "write"] = "write" if name in _WRITE_TOOLS else "read"
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
                    raw_text=content,
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
                raw = body if isinstance(body, str) else json.dumps(body, default=str)
                out.append(
                    Event(
                        kind=EventKind.TOOL_RESULT,
                        text=_clip(raw),
                        raw_text=raw,
                        ts=ts,
                        raw_index=index,
                    )
                )
            elif block.get("type") == "text":
                text = block.get("text")
                raw = text if isinstance(text, str) else ""
                out.append(
                    Event(
                        kind=EventKind.USER,
                        text=_clip(raw),
                        raw_text=raw,
                        ts=ts,
                        raw_index=index,
                    )
                )
        return out

    def _trajectory_facts(  # noqa: PLR0912, PLR0915
        self, record: dict, index: int
    ) -> list[TrajectoryFact]:
        timestamp = _epoch(record.get("timestamp"))
        timing = _claude_timing(record, timestamp)
        record_id = _trajectory_id(record.get("uuid") or record.get("id"))
        turn_id = _trajectory_id(
            record.get("turn_id") or record.get("turnId") or record.get("promptId")
        )
        step_id = _trajectory_id(record.get("step_id") or record.get("stepId"))
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
                    source="claude",
                    summary=_safe_trajectory_text(summary),
                    status=status,
                    native_id=clean_id,
                    revision=_claude_revision(record),
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

        kind = record.get("type")
        if kind == "assistant":
            message = record.get("message")
            message = message if isinstance(message, dict) else {}
            message_id = _trajectory_id(message.get("id"))
            message_turn = (
                _trajectory_id(message.get("turn_id") or message.get("turnId"))
                or message_id
                or _trajectory_id(record.get("requestId"))
                or turn_id
            )
            stop = message.get("stop_reason")
            message_status = _trajectory_status(
                message.get("status") or record.get("status"), TrajectoryStatus.COMPLETED
            )
            usage = _claude_trajectory_usage(message, record)
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content}]
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                native_id = _claude_block_native_id(block, message_id, record_id, block_index)
                parent_call_id = _trajectory_id(
                    block.get("parent_call_id") or block.get("parentCallId")
                )
                if block_type == "text":
                    raw = _safe_trajectory_text(block.get("text"))
                    add(
                        TrajectoryKind.ASSISTANT,
                        TrajectoryLane.MODEL,
                        raw,
                        native_id=native_id,
                        status=message_status,
                        turn=message_turn,
                    )
                elif block_type == "thinking":
                    raw = _safe_trajectory_text(block.get("thinking"))
                    if not isinstance(block.get("thinking"), str):
                        continue
                    add(
                        TrajectoryKind.REASONING,
                        TrajectoryLane.MODEL,
                        raw,
                        native_id=native_id,
                        status=_trajectory_status(block.get("status"), message_status),
                        turn=message_turn,
                        details=(_trajectory_detail("thinking", raw, format=ContentFormat.TEXT),),
                    )
                elif block_type in ("tool_use", "server_tool_use"):
                    name = _safe_trajectory_text(block.get("name"))
                    call_id = _trajectory_id(block.get("id") or block.get("call_id"))
                    input_value = block.get("input")
                    block_details = (
                        (_trajectory_detail("input", input_value, format=ContentFormat.JSON),)
                        if input_value is not None
                        else ()
                    )
                    add(
                        TrajectoryKind.TOOL_CALL,
                        TrajectoryLane.TOOLS,
                        name or "tool call",
                        native_id=native_id,
                        status=_trajectory_status(block.get("status"), TrajectoryStatus.PENDING),
                        turn=message_turn,
                        call_id=call_id,
                        parent_call_id=parent_call_id,
                        details=block_details,
                    )
                elif block_type == "tool_result":
                    raw = _claude_content_text(block.get("content"))
                    call_id = _trajectory_id(block.get("tool_use_id") or block.get("call_id"))
                    result_status = (
                        TrajectoryStatus.ERROR
                        if block.get("is_error") is True
                        else _trajectory_status(block.get("status"), TrajectoryStatus.COMPLETED)
                    )
                    result_details = (
                        (
                            _trajectory_detail(
                                "result",
                                block.get("content"),
                                format=(
                                    ContentFormat.TEXT
                                    if isinstance(block.get("content"), str)
                                    else ContentFormat.JSON
                                ),
                            ),
                        )
                        if block.get("content") is not None
                        else ()
                    )
                    add(
                        TrajectoryKind.TOOL_RESULT,
                        TrajectoryLane.TOOLS,
                        raw,
                        native_id=native_id,
                        status=result_status,
                        turn=message_turn,
                        call_id=call_id,
                        parent_call_id=parent_call_id or call_id,
                        details=result_details,
                    )
            if not facts and (usage is not None or stop is not None):
                add(
                    TrajectoryKind.ASSISTANT,
                    TrajectoryLane.MODEL,
                    native_id=message_id or record_id,
                    status=message_status,
                    turn=message_turn,
                )
            if usage is not None and facts:
                facts[-1] = replace(facts[-1], usage=usage)
            return facts

        if kind == "user":
            message = record.get("message")
            message = message if isinstance(message, dict) else {}
            message_id = _trajectory_id(message.get("id"))
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content}]
            user_status = _trajectory_status(
                message.get("status") or record.get("status"), TrajectoryStatus.COMPLETED
            )
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                native_id = _claude_block_native_id(block, message_id, record_id, block_index)
                if block_type == "text":
                    add(
                        TrajectoryKind.USER,
                        TrajectoryLane.INPUT,
                        _safe_trajectory_text(block.get("text")),
                        native_id=native_id,
                        status=_trajectory_status(block.get("status"), user_status),
                    )
                elif block_type == "tool_result":
                    raw = _claude_content_text(block.get("content"))
                    call_id = _trajectory_id(block.get("tool_use_id") or block.get("call_id"))
                    parent_call_id = _trajectory_id(
                        block.get("parent_call_id") or block.get("parentCallId")
                    )
                    result_status = (
                        TrajectoryStatus.ERROR
                        if block.get("is_error") is True
                        else _trajectory_status(block.get("status"), TrajectoryStatus.COMPLETED)
                    )
                    details = (
                        (
                            _trajectory_detail(
                                "result",
                                block.get("content"),
                                format=(
                                    ContentFormat.TEXT
                                    if isinstance(block.get("content"), str)
                                    else ContentFormat.JSON
                                ),
                            ),
                        )
                        if block.get("content") is not None
                        else ()
                    )
                    add(
                        TrajectoryKind.TOOL_RESULT,
                        TrajectoryLane.TOOLS,
                        raw,
                        native_id=native_id,
                        status=result_status,
                        call_id=call_id,
                        parent_call_id=parent_call_id or call_id,
                        details=details,
                    )
            if not facts and (isinstance(content, str) or record_id is not None):
                add(
                    TrajectoryKind.USER,
                    TrajectoryLane.INPUT,
                    _safe_trajectory_text(content),
                    native_id=message_id or record_id,
                    status=user_status,
                )
            return facts

        if kind in ("system", "context", "summary"):
            error = record.get("error")
            subtype = _safe_trajectory_text(record.get("subtype") or record.get("type"))
            is_error = record.get("level") == "error"
            fact_kind = (
                TrajectoryKind.ERROR
                if is_error
                else TrajectoryKind.CONTEXT
                if kind in ("context", "summary")
                or any(token in subtype for token in ("context", "compact"))
                else TrajectoryKind.SYSTEM
            )
            body = error if is_error else record.get("content")
            if body is None:
                body = record.get("summary") or record.get("message") or subtype
            summary = _claude_content_text(body)
            system_details: list[DetailField] = []
            if error is not None:
                system_details.append(_trajectory_detail("error", error, format=ContentFormat.TEXT))
            if record.get("content") is not None and not isinstance(record.get("content"), str):
                system_details.append(
                    _trajectory_detail("content", record.get("content"), format=ContentFormat.JSON)
                )
            if record.get("durationMs") is not None:
                system_details.append(
                    _trajectory_detail(
                        "duration_ms", record.get("durationMs"), format=ContentFormat.JSON
                    )
                )
            metadata = {
                key: value
                for key, value in record.items()
                if key not in {"content", "message", "error", "timestamp", "type"}
            }
            if metadata:
                system_details.append(
                    _trajectory_detail("metadata", metadata, format=ContentFormat.JSON)
                )
            add(
                fact_kind,
                TrajectoryLane.THEATER
                if fact_kind is TrajectoryKind.ERROR
                else TrajectoryLane.MODEL,
                summary,
                native_id=record_id,
                status=(
                    TrajectoryStatus.ERROR
                    if is_error
                    else _trajectory_status(record.get("status"), TrajectoryStatus.COMPLETED)
                ),
                details=tuple(system_details),
            )
        return facts

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
                    # Sidechain records have no id; root is first with no parentUuid.
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
            return ScreenReading(kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH)
        if APPROVAL_MARKER in capture:
            return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
        lines = [line for line in capture.splitlines() if line.strip()]
        tail = lines[-_SCREEN_TAIL_LINES:]
        if any(WORKING_MARKER in line for line in tail):
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        if any(IDLE_FOOTER in line for line in tail):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        if any(
            line.strip().startswith(MODE_LINE_PREFIXES)
            and line.rstrip().endswith(IDLE_AGENTS_FOOTER)
            for line in tail
        ):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)


#: What the loader looks for. An instance, not the class: see docs/harness-plugins.md.
HARNESS = ClaudeCodeHarness()
