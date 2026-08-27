"""Transcript identity commands: receipt ingestion, Claude alias, candidates, bind."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from theater.cli.render import _candidate_line
from theater.client import DaemonClient, call_sync
from theater.constants.harness import (
    HARNESS_CHANNEL_DEFAULT_MAX_PAYLOAD_BYTES,
    HARNESS_HOOK_TOKEN_MAX_CHARS,
)
from theater.harness.channels.hooks import (
    HookIngressError,
    validate_hook_identifier,
    validate_hook_payload,
)
from theater.protocol import RemoteError


def _hook_string(data: dict, *names: str) -> str | None:
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value:
            return value
    return None


async def _send_transcript_receipt(args, *, token: str, payload: dict) -> None:
    async with DaemonClient(autostart=False) as client:
        await client.call(
            "transcript.receipt",
            id=args.id,
            token=token,
            payload=payload,
        )


async def _send_harness_event(args, *, token: str, payload: dict) -> None:
    async with DaemonClient(autostart=False) as client:
        await client.call(
            "harness.event",
            id=args.id,
            token=token,
            channel=args.channel,
            event=args.event,
            delivery_id=args.delivery_id,
            payload=payload,
        )


def cmd_harness_event(args) -> int:
    """Forward one bounded generic hook payload without autostarting the daemon."""
    strict = int(bool(getattr(args, "strict_exit", False)))
    try:
        stream = getattr(sys.stdin, "buffer", None)
        raw = (
            stream.read(HARNESS_CHANNEL_DEFAULT_MAX_PAYLOAD_BYTES + 1)
            if stream is not None
            else sys.stdin.read(HARNESS_CHANNEL_DEFAULT_MAX_PAYLOAD_BYTES + 1).encode("utf-8")
        )
        if len(raw) > HARNESS_CHANNEL_DEFAULT_MAX_PAYLOAD_BYTES:
            return strict
        payload = json.loads(raw.decode("utf-8"))
        payload = validate_hook_payload(
            payload, max_bytes=HARNESS_CHANNEL_DEFAULT_MAX_PAYLOAD_BYTES
        )
        validate_hook_identifier(args.event, "event")
        validate_hook_identifier(args.channel, "channel")
        if args.delivery_id is not None:
            validate_hook_identifier(args.delivery_id, "delivery_id")
        with Path(args.token_file).open("rb") as token_file:
            if os.fstat(token_file.fileno()).st_size > HARNESS_HOOK_TOKEN_MAX_CHARS + 1:
                return strict
            token_raw = token_file.read(HARNESS_HOOK_TOKEN_MAX_CHARS + 1)
        token = token_raw.decode("utf-8").strip()
        if not token or len(token) > HARNESS_HOOK_TOKEN_MAX_CHARS:
            return strict
    except (OSError, UnicodeDecodeError, ValueError, HookIngressError):
        return strict
    try:
        asyncio.run(_send_harness_event(args, token=token, payload=payload))
    except Exception:
        return strict
    return 0


def cmd_transcript_receipt(args) -> int:
    """Ingest a lifecycle hook receipt and forward the payload untouched.

    Hidden from normal CLI help. The hook provides JSON on stdin; the launch
    token lives in a daemon-written file so it is not exposed in participant
    rows, transcripts, or argv.
    """
    try:
        token = Path(args.token_file).read_text().strip()
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        return int(bool(getattr(args, "strict_exit", False)))
    if not isinstance(payload, dict):
        return int(bool(getattr(args, "strict_exit", False)))
    try:
        asyncio.run(_send_transcript_receipt(args, token=token, payload=payload))
    except Exception:
        return int(bool(getattr(args, "strict_exit", False)))
    return 0


def cmd_claude_receipt(args) -> int:
    """Backward-compatible alias for the Claude-specific hook command.

    Shipped in v3.2.0; live Claude sessions have settings.json invoking
    ``claude-receipt`` by that exact name. Extracts ``session_id`` and
    ``transcript_path`` from stdin JSON and wraps them into a payload before
    forwarding to the generic ``transcript.receipt`` RPC.
    """
    try:
        token = Path(args.token_file).read_text().strip()
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = _hook_string(payload, "session_id", "sessionId")
    transcript_path = _hook_string(payload, "transcript_path", "transcriptPath")
    if session_id is None or transcript_path is None:
        return 0
    forwarded = {"session_id": session_id, "transcript_path": transcript_path}
    try:
        asyncio.run(_send_transcript_receipt(args, token=token, payload=forwarded))
    except (RemoteError, ConnectionError, OSError):
        return 0
    return 0


def cmd_candidates(args) -> int:
    data = call_sync("transcript.candidates", id=args.id)
    assert isinstance(data, dict)
    rows = data.get("candidates") or []
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    if not rows:
        print("no candidates")
        return 0
    print(
        f"{'LOCATION':<72} {'SESSION':<36} {'MTIME':<17} {'SIZE':>9} "
        f"{'OWNER':<14} {'TOMBSTONE':<14} REJECTION"
    )
    for row in rows:
        print(_candidate_line(row))
    return 0


def cmd_bind(args) -> int:
    record = call_sync(
        "transcript.bind",
        id=args.id,
        candidate=args.candidate,
        confirm_id=args.confirm_id,
        transfer_from=args.transfer_from,
        transfer_confirm_id=args.transfer_confirm_id,
    )
    assert isinstance(record, dict)
    prior = f" (transferred from {record['prior_owner']})" if record.get("prior_owner") else ""
    print(f"bound {record['id']} -> {record['location']}{prior}")
    return 0
