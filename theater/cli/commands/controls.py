"""Control commands: steer, queue, settings, controls, interrupt.

Thin client calls, matching the rest of the CLI: policy belongs to the
daemon, so these commands forward ``caller_id="cli"`` — the local-operator
identity the daemon already trusts for kills — and print exactly what the
daemon answered, including its reasons for unsupported actions. No control
policy is decided client-side.
"""

from __future__ import annotations

import json
import sys

from theater.client import call_sync

#: The CLI is the local operator. MCP forwards the actual calling
#: participant instead; the daemon authorizes both.
_CLI_CALLER_ID = "cli"


def _print_json(record) -> None:
    print(json.dumps(record, indent=2))


def cmd_steer(args) -> int:
    record = call_sync(
        "participant.steer",
        target=args.target,
        prompt=args.prompt,
        caller_id=_CLI_CALLER_ID,
        job_handle=args.job_handle,
    )
    assert isinstance(record, dict)
    if args.json:
        _print_json(record)
    else:
        print(f"steered {args.target} — amended job {record['handle']}")
    return 0


def cmd_queue(args) -> int:
    record = call_sync(
        "participant.queue_followup",
        target=args.target,
        prompt=args.prompt,
        caller_id=_CLI_CALLER_ID,
    )
    assert isinstance(record, dict)
    if args.json:
        _print_json(record)
    else:
        print(f"queued {args.target} — followup job {record['handle']}")
    return 0


def cmd_settings(args) -> int:
    record = call_sync(
        "participant.settings.update",
        target=args.target,
        caller_id=_CLI_CALLER_ID,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    assert isinstance(record, dict)
    applied = record.get("applied")
    if args.json:
        _print_json(record)
    elif applied is True:
        model = record.get("model")
        reasoning = record.get("reasoning_effort")
        print(
            f"settings applied for {args.target}: "
            f"model={model if model is not None else '-'} "
            f"reasoning_effort={reasoning if reasoning is not None else '-'}"
        )
    elif applied is False:
        code = record.get("error_code") or "settings_rejected"
        print(f"theater: {code}: {record.get('error')}", file=sys.stderr)
    else:
        # Uncertain application stays visibly uncertain, never optimistic.
        code = record.get("error_code") or "delivery_unknown"
        print(f"theater: {code}: {record.get('error')}", file=sys.stderr)
    return 0 if applied is True else 1


def cmd_controls(args) -> int:
    record = call_sync("participant.controls", target=args.target)
    assert isinstance(record, dict)
    if args.json:
        _print_json(record)
        return 0
    for line in _render_controls(record):
        print(line)
    return 0


def _render_controls(record: dict) -> list[str]:
    """Human rendering of one daemon controls answer, line by line."""
    target = record.get("id")
    wiring = record.get("wiring")
    health = record.get("health")
    connection = health.get("connection") if isinstance(health, dict) else None
    lines = [f"{target}  wiring={wiring}  connection={connection or '-'}"]

    settings = record.get("settings")
    if isinstance(settings, dict):
        model = settings.get("model")
        reasoning = settings.get("reasoning_effort")
        lines.append(
            f"settings: model={model if model is not None else '-'} "
            f"reasoning_effort={reasoning if reasoning is not None else '-'}"
        )
    else:
        lines.append("settings: fixed at launch (no native runtime wiring)")

    turn = record.get("active_turn")
    if isinstance(turn, dict):
        job = turn.get("job_handle")
        lines.append(
            f"active turn: {turn.get('native_turn_id')} "
            f"job={job if job is not None else 'human turn'}"
        )
        if isinstance(turn.get("pending_interaction"), dict):
            interaction = turn["pending_interaction"]
            lines.append(
                f"pending {interaction.get('kind')}: answer it in the native UI"
                + (f" — {interaction['details']}" if interaction.get("details") else "")
            )
    else:
        lines.append("active turn: none")

    queued = record.get("queued") or []
    if queued:
        lines.append(f"queued followups ({len(queued)}): {' '.join(queued)}")
    else:
        lines.append("queued followups: none")

    capabilities = record.get("capabilities") or {}
    for name, entry in capabilities.items():
        assert isinstance(entry, dict)
        if entry.get("available"):
            lines.append(f"  {name:<16} available")
            continue
        reason = entry.get("reason") or "not_determined"
        detail = entry.get("detail")
        lines.append(f"  {name:<16} unavailable ({reason})" + (f" — {detail}" if detail else ""))
    return lines


def cmd_interrupt(args) -> int:
    record = call_sync(
        "participant.interrupt",
        target=args.target,
        caller_id=_CLI_CALLER_ID,
    )
    assert isinstance(record, dict)
    if args.json:
        _print_json(record)
    elif record.get("interrupted"):
        cancelled = record.get("cancelled_followups") or []
        suffix = f" (cancelled {len(cancelled)} queued followups)" if cancelled else ""
        print(f"interrupted {args.target}{suffix}")
    else:
        print(f"did not interrupt {args.target} ({record.get('reason')})")
    return 0
