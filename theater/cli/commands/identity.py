"""Transcript identity commands: receipt ingestion, candidates, and binding."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from theater.cli.render import _candidate_line
from theater.client import DaemonClient, call_sync
from theater.protocol import RemoteError


async def _send_transcript_receipt(args, *, token: str, payload: dict) -> None:
    async with DaemonClient(autostart=False) as client:
        await client.call(
            "transcript.receipt",
            id=args.id,
            token=token,
            payload=payload,
        )


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
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        asyncio.run(_send_transcript_receipt(args, token=token, payload=payload))
    except (RemoteError, ConnectionError, OSError):
        return 0
    return 0


cmd_claude_receipt = cmd_transcript_receipt


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
