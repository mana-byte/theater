"""Claude receipt token files, their cleanup and the receipt command."""

from __future__ import annotations

import io
import json
import stat
import time
from pathlib import Path

import pytest

from theater import cli, paths
from theater.cli.commands import identity as identity_mod
from theater.protocol import RemoteError


def test_live_receipt_tokens_ignore_legacy_expiry(registry, tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    token_file = paths.participant_observation_dir("p-claude", "claude") / "receipt-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("secret")
    p = registry.create_spawned(harness="claude", cwd=str(cwd), pid="p-claude")
    registry.store.set_meta(
        f"receipt_token:{p.id}",
        json.dumps(
            {"token": "secret", "token_path": str(token_file), "expires_at": time.time() - 1}
        ),
    )

    assert registry.store.get_receipt_token(p.id) == "secret"
    assert token_file.exists()


def test_mark_dead_removes_receipt_token_file(registry, tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    token_file = paths.participant_observation_dir("p-claude", "claude") / "receipt-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("secret")
    p = registry.create_spawned(harness="claude", cwd=str(cwd), pid="p-claude")
    registry.store.set_receipt_token(p.id, "secret", token_path=str(token_file))

    registry.mark_dead(p.id)

    assert registry.store.get_meta(f"receipt_token:{p.id}") is None
    assert not token_file.exists()


async def test_gc_removes_orphaned_receipt_tokens(store, tmp_path):
    from theater.config import RetentionSection
    from theater.daemon.gc import sweep

    token_file = paths.participant_observation_dir("missing", "claude") / "receipt-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("secret")
    store.set_receipt_token("missing", "secret", token_path=str(token_file))

    await sweep(store, RetentionSection())

    assert store.get_meta("receipt_token:missing") is None
    assert not token_file.exists()


async def test_gc_removes_dead_participant_receipt_tokens(registry, tmp_path):
    from theater.config import RetentionSection
    from theater.daemon.gc import sweep

    cwd = tmp_path / "repo"
    cwd.mkdir()
    token_file = paths.participant_observation_dir("p-claude", "claude") / "receipt-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("secret")
    p = registry.create_spawned(harness="claude", cwd=str(cwd), pid="p-claude")
    registry.store.set_receipt_token(p.id, "secret", token_path=str(token_file))
    registry.store.set_status(p.id, "dead")

    await sweep(registry.store, RetentionSection())

    assert registry.store.get_meta(f"receipt_token:{p.id}") is None
    assert not token_file.exists()


def test_private_token_file_is_restricted_even_when_preexisting(registry, tmp_path):
    from theater.daemon.spawning.service import Spawner
    from theater.harness.base import LaunchPlan

    token_file = paths.participant_observation_dir("p-claude", "claude") / "receipt-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.parent.chmod(0o755)
    token_file.write_text("old")
    token_file.chmod(0o644)

    Spawner(registry)._write_plan_files(
        LaunchPlan(argv=[], receipt_token="new", receipt_token_path=token_file)
    )

    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_claude_receipt_cli_is_quiet_and_does_not_autostart(monkeypatch, capsys, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    args = cli._parser().parse_args(
        ["claude-receipt", "--id", "p-claude", "--token-file", str(token_file)]
    )
    monkeypatch.setattr("sys.stdin", type("Input", (), {"read": lambda self: "not-json"})())

    assert cli.cmd_claude_receipt(args) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


def _receipt_args(token_file: Path):
    return cli._parser().parse_args(
        ["claude-receipt", "--id", "p-claude", "--token-file", str(token_file)]
    )


def _receipt_stdin(path: Path) -> io.StringIO:
    return io.StringIO(
        json.dumps(
            {
                "session_id": path.stem,
                "transcript_path": str(path),
            }
        )
    )


def test_claude_receipt_cli_uses_non_autostart_client(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    transcript = tmp_path / "11111111-1111-4111-8111-111111111111.jsonl"
    payload = {
        "session_id": transcript.stem,
        "transcript_path": str(transcript),
        "hook_event_name": "SessionStart",
        "native": {"future": True},
    }
    seen = []

    class Client:
        def __init__(self, *, autostart=True):
            seen.append(("autostart", autostart))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def call(self, method, **params):
            seen.append((method, params))
            return {"ok": True}

    monkeypatch.setattr(identity_mod, "DaemonClient", Client)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert cli.cmd_claude_receipt(_receipt_args(token_file)) == 0
    assert seen == [
        ("autostart", False),
        (
            "transcript.receipt",
            {"id": "p-claude", "token": "secret", "payload": payload},
        ),
    ]


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("no daemon"),
        RemoteError("bad_request", "rejected"),
    ],
)
def test_claude_receipt_cli_is_quiet_when_daemon_cannot_accept(monkeypatch, capsys, tmp_path, exc):
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    transcript = tmp_path / "11111111-1111-4111-8111-111111111111.jsonl"

    async def reject(*args, **kwargs):
        raise exc

    monkeypatch.setattr(identity_mod, "_send_transcript_receipt", reject)
    monkeypatch.setattr("sys.stdin", _receipt_stdin(transcript))

    assert cli.cmd_claude_receipt(_receipt_args(token_file)) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""
