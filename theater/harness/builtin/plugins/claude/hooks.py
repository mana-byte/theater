"""Supported Claude Code tool-lifecycle hook bindings.

These command hooks are observation-only.  They run asynchronously and use the
generic authenticated ingress, so a missing daemon or malformed delivery never
blocks Claude's native tool execution.  The durable transcript remains the
primary source for tool input, results, turn completion, and usage.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path

from theater import paths
from theater.harness.base import theater_binary
from theater.harness.contracts.callbacks import (
    HookCorrelationContext,
    HookDecodeContext,
    HookInstallContext,
    HookInstallOverlay,
)
from theater.harness.contracts.channels import ChannelFact, SignalKind
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.normalization.values import (
    finite_float,
    trajectory_detail,
    trajectory_identifier,
)
from theater.provenance import is_trusted_provenance
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import (
    TimingProvenance,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)
from theater.trajectory.records import Timing, TrajectoryFailure
from theater.transcript_identity import canonical_location

NATIVE_HOOK_CHANNEL = "native-hooks"
CLAUDE_TOOL_HOOK_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")

type ClaudeHook = dict[str, object]
type ClaudeHookEntry = dict[str, list[ClaudeHook]]
type ClaudeHookEvents = dict[str, list[ClaudeHookEntry]]
type ClaudeSettings = dict[str, ClaudeHookEvents]

_HOOK_SOURCE = "claude-hook"


def native_hook_token_path(participant_id: str) -> Path:
    """Return the generic installer token path known to Claude's settings builder."""
    return paths.participant_observation_dir(participant_id, "claude") / (
        f"hook-{NATIVE_HOOK_CHANNEL}.token"
    )


def native_hook_settings(participant_id: str) -> ClaudeSettings:
    """Build stock Claude ``--settings`` entries for asynchronous observations.

    ``harness-event`` deliberately has no ``--strict-exit`` flag: ingress
    unavailability and malformed input must leave native tool execution alone.
    The generic installer mints the referenced token after this pure plan is
    built; its deterministic path is validated by :func:`install_native_hooks`.
    """
    return {
        "hooks": {
            event: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _native_hook_command(participant_id, event),
                            "async": True,
                        }
                    ]
                }
            ]
            for event in CLAUDE_TOOL_HOOK_EVENTS
        }
    }


def install_native_hooks(context: HookInstallContext) -> HookInstallOverlay:
    """Validate the launcher/installer token join and request the credential.

    Claude's sole settings file is already owned by the legacy launch planner,
    which composes receipt and native-hook commands before generic installation.
    The installer therefore needs no overlay files or environment variables;
    returning an empty overlay still causes core to mint and write the
    authenticated channel credential.
    """
    if context.channel_id != NATIVE_HOOK_CHANNEL:
        raise ValueError(f"Claude native hook channel must be {NATIVE_HOOK_CHANNEL!r}")
    expected = native_hook_token_path(context.participant_id)
    if context.token_file != expected:
        raise ValueError("Claude native hook credential does not match the launch-local command")
    return HookInstallOverlay()


def correlate_tool_hook(context: HookCorrelationContext) -> str:
    """Return one admitted stable identity for a tool lifecycle observation."""
    payload = context.payload
    expected_session_id, expected_transcript_location = _trusted_identity(context)
    _require_event_name(payload, context.event)
    session_id = _require_identifier(payload, "session_id")
    transcript_location = _require_transcript_for_session(payload, session_id)
    if session_id != expected_session_id:
        raise ValueError("Claude hook session_id does not match the trusted session")
    if transcript_location != expected_transcript_location:
        raise ValueError("Claude hook transcript_path does not match the trusted transcript")
    tool_use_id = _require_identifier(payload, "tool_use_id")
    native_id = trajectory_identifier(
        f"claude-hook:{session_id}:{tool_use_id}", overflow_prefix="claude-hook"
    )
    if native_id is None:
        raise ValueError("Claude hook session/tool identity is invalid")
    return native_id


def decode_tool_hook(context: HookDecodeContext) -> tuple[ChannelFact, ...]:
    """Project one bounded, non-authoritative native lifecycle observation."""
    payload = context.payload
    _require_event_name(payload, context.event)
    session_id = _require_identifier(payload, "session_id")
    transcript_path = _require_transcript_for_session(payload, session_id)
    tool_use_id = _require_identifier(payload, "tool_use_id")
    expected_native_id = trajectory_identifier(
        f"claude-hook:{session_id}:{tool_use_id}", overflow_prefix="claude-hook"
    )
    if expected_native_id is None or context.native_id != expected_native_id:
        raise ValueError("Claude hook native identity does not match its session/tool payload")
    tool_name = _require_identifier(payload, "tool_name")
    if not isinstance(payload.get("tool_input"), Mapping):
        raise TypeError("Claude hook payload is missing tool_input")

    prompt_id = _optional_identifier(payload, "prompt_id")
    details = [
        trajectory_detail("hook_event", context.event, format=ContentFormat.TEXT),
        trajectory_detail("session_id", session_id, format=ContentFormat.TEXT),
        trajectory_detail("transcript_path", transcript_path, format=ContentFormat.PATH),
        trajectory_detail("tool_use_id", tool_use_id, format=ContentFormat.TEXT),
    ]
    if prompt_id is not None:
        details.append(trajectory_detail("prompt_id", prompt_id, format=ContentFormat.TEXT))

    status, revision, timing, failure = _terminal_projection(context.event, payload, details)
    summary = f"Claude hook: {tool_name} {_event_summary(context.event)}"
    fact = TrajectoryFact(
        kind=TrajectoryKind.SYSTEM,
        lane=TrajectoryLane.TOOLS,
        source=_HOOK_SOURCE,
        summary=summary,
        status=status,
        native_id=context.native_id,
        revision=revision,
        timing=timing,
        failure=failure,
        details=tuple(details),
    )
    # Do not emit TOOL_CALL/TOOL_RESULT or usage facts here.  The transcript
    # owns those durable facts and may arrive after this best-effort hook.
    return (ChannelFact(SignalKind.LIFECYCLE, fact),)


def _native_hook_command(participant_id: str, event: str) -> str:
    return shlex.join(
        [
            theater_binary(),
            "harness-event",
            event,
            "--id",
            participant_id,
            "--channel",
            NATIVE_HOOK_CHANNEL,
            "--token-file",
            str(native_hook_token_path(participant_id)),
        ]
    )


def _terminal_projection(
    event: str,
    payload: Mapping[str, object],
    details: list[DetailField],
) -> tuple[TrajectoryStatus, int, Timing | None, TrajectoryFailure | None]:
    if event == "PreToolUse":
        return TrajectoryStatus.RUNNING, 0, None, None

    duration_ms = finite_float(payload.get("duration_ms"))
    timing = (
        Timing(duration_ms=duration_ms, provenance=TimingProvenance.SOURCE)
        if duration_ms is not None and duration_ms >= 0
        else None
    )
    if event == "PostToolUse":
        return TrajectoryStatus.COMPLETED, 1, timing, None
    if event != "PostToolUseFailure":
        raise ValueError(f"Claude hook event {event!r} has no lifecycle projection")

    error = _require_text(payload, "error")
    is_interrupt = payload.get("is_interrupt", False)
    if not isinstance(is_interrupt, bool):
        raise TypeError("Claude PostToolUseFailure is_interrupt must be a boolean")
    details.append(trajectory_detail("error", error, format=ContentFormat.TEXT))
    details.append(trajectory_detail("is_interrupt", is_interrupt, format=ContentFormat.JSON))
    if is_interrupt:
        return TrajectoryStatus.INTERRUPTED, 1, timing, None
    return (
        TrajectoryStatus.ERROR,
        1,
        timing,
        TrajectoryFailure(TrajectoryFailureCategory.TOOL, detail=error),
    )


def _event_summary(event: str) -> str:
    return {
        "PreToolUse": "started",
        "PostToolUse": "completed",
        "PostToolUseFailure": "failed",
    }.get(event, "changed")


def _require_event_name(payload: Mapping[str, object], event: str) -> None:
    if payload.get("hook_event_name") != event:
        raise ValueError("Claude hook_event_name does not match the declared binding")


def _require_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Claude hook payload is missing {key}")
    return value


def _require_identifier(payload: Mapping[str, object], key: str) -> str:
    value = trajectory_identifier(payload.get(key))
    if value is None:
        raise ValueError(f"Claude hook payload has invalid {key}")
    return value


def _trusted_identity(context: HookCorrelationContext) -> tuple[str, str]:
    """Require one daemon-trusted, non-quarantined session/transcript join."""
    if context.identity_lost:
        raise ValueError("Claude hook transcript identity is quarantined")
    if not is_trusted_provenance(context.expected_session_provenance):
        raise ValueError("Claude hook has no trusted transcript identity")
    session_id = trajectory_identifier(context.expected_session_id)
    if session_id is None:
        raise ValueError("Claude hook has no trusted expected session")
    location = _canonical_transcript_for_session(
        context.expected_transcript_location,
        session_id,
        label="trusted transcript",
    )
    return session_id, location


def _require_transcript_for_session(payload: Mapping[str, object], session_id: str) -> str:
    """Require Claude's captured transcript path to agree with its session id."""
    return _canonical_transcript_for_session(
        payload.get("transcript_path"), session_id, label="transcript_path"
    )


def _canonical_transcript_for_session(value: object, session_id: str, *, label: str) -> str:
    """Return one absolute canonical Claude transcript path for ``session_id``."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"Claude hook payload is missing {label}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Claude hook {label} must be an absolute transcript path")
    transcript_path = canonical_location(value)
    canonical = Path(transcript_path)
    if canonical.suffix != ".jsonl" or canonical.stem != session_id:
        raise ValueError(f"Claude hook {label} does not match session_id")
    return transcript_path


def _optional_identifier(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    resolved = trajectory_identifier(value)
    if resolved is None:
        raise ValueError(f"Claude hook payload has invalid {key}")
    return resolved


__all__ = [
    "CLAUDE_TOOL_HOOK_EVENTS",
    "NATIVE_HOOK_CHANNEL",
    "correlate_tool_hook",
    "decode_tool_hook",
    "install_native_hooks",
    "native_hook_settings",
    "native_hook_token_path",
]
