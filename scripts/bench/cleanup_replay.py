"""Replay historical workspace cleanup failures and a real-shaped Vibe failed turn (WP-B67).

Runs against whichever `theater` is importable, so the same script measures both sides:
  uv run python scripts/bench/cleanup_replay.py                       # after (this branch)
  uv run --project /tmp/base-b67 python scripts/bench/cleanup_replay.py  # before (4104dca4)
Reads theater.db read-only; every Git fixture and Vibe store lives in a temp dir.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import theater
from theater.daemon.worktrees import cleanup as cleanup_module
from theater.daemon.worktrees.identity import GitFactsError
from theater.models import WorkspaceRecord

REPO_ROOT = Path(theater.__file__).resolve().parents[1]
DB = Path(os.environ.get("THEATER_HOME", Path.home() / ".theater")) / "var/state/theater.db"

# Sanitized copy of one real vibe latestTurn.error (Oct 2026, generation projection-state.json).
VIBE_FAILED_TURN_ERROR = {
    "code": "model_stream_failed",
    "details": {
        "exceptionType": "ReadTimeout",
        "model": "zai-glm-5-3",
        "provider": "mistral",
        "purpose": "agent",
    },
    "message": "",
}


def _git(cwd: Path | str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _classify(row_error: dict) -> str:
    details = row_error.get("details") or {}
    first = (details.get("errors") or [""])[0]
    if "not fully merged" in first:
        return "unmerged"
    if "different branch checked out" in first:
        return "branch_moved"
    if first.startswith("worktree remove:"):
        return "dirty"
    return "other"


def _historical_rows() -> Counter[str]:
    if not DB.exists():
        return Counter()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select error from public_operations where kind='workspace_cleanup' and state='failed'"
        ).fetchall()
    finally:
        conn.close()
    return Counter(_classify(json.loads(error or "{}")) for (error,) in rows)


def _fixture(root: Path, scenario: str) -> WorkspaceRecord:
    repo = root / scenario / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "bench@example.com")
    _git(repo, "config", "user.name", "Bench")
    (repo / "README.md").write_text("bench\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")
    path = root / scenario / "worktree"
    branch = f"theater/{scenario}"
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
    if scenario == "unmerged":
        (path / "work.txt").write_text("child work\n")
        _git(path, "add", "work.txt")
        _git(path, "commit", "-q", "-m", "child work")
    elif scenario == "branch_moved":
        _git(path, "checkout", "-q", "-b", "child-switched")
    elif scenario == "dirty":
        (path / "scratch.txt").write_text("uncommitted\n")
    return WorkspaceRecord(
        workspace_id=f"ws-{scenario}",
        ownership_kind="theater",
        owner_id="bench",
        path=str(path.resolve()),
        state="active",
        created_at=0.0,
        updated_at=0.0,
        canonical_repository_root=str(repo.resolve()),
        branch=branch,
        resolved_base_commit=_git(repo, "rev-parse", "HEAD"),
    )


def _cleanup(record: WorkspaceRecord, *, force: bool) -> dict[str, object]:
    """Mirror cleanup_service: ok settles as succeeded, a GitFactsError as failed."""
    try:
        result = cleanup_module.cleanup_exact_worktree(
            record, force=force, delete_branch=True, force_branch=False
        )
    except GitFactsError as exc:
        refused = getattr(cleanup_module, "refused_cleanup", None)
        wire = refused(exc).to_wire() if refused else {"errors": [str(exc)]}
        return {"state": "failed", **wire}
    return {"state": "succeeded" if result.ok else "failed", **result.to_wire()}


def bench_cleanup() -> list[tuple[str, str, str]]:
    history = _historical_rows()
    rows: list[tuple[str, str, str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        outcomes = {
            scenario: _cleanup(_fixture(root, scenario), force=False)
            for scenario in ("unmerged", "branch_moved", "dirty")
        }
        failed_after = sum(
            count
            for category, count in history.items()
            if category not in outcomes or outcomes[category]["state"] == "failed"
        )
        rows.append(("B6 failed cleanup rows (historical)", "failed", str(failed_after)))
        for category in ("unmerged", "branch_moved", "dirty", "other"):
            outcome = outcomes.get(category, {"state": "failed (not replayed)"})
            rows.append(
                (f"B6 {category} x{history.get(category, 0)}", "settles as", str(outcome["state"]))
            )
        moved = outcomes["branch_moved"]
        rows.append(("B6 branch_moved refusal", "details.next", str(moved.get("next"))))
        record = _fixture(root / "forced", "branch_moved")
        forced = _cleanup(record, force=True)
        repo = record.canonical_repository_root
        assert repo is not None
        kept = _git(repo, "branch", "--list", "child-switched")
        rows.append(
            (
                "B6 branch_moved --force",
                "state / worktree gone / moved branch kept",
                f"{forced['state']} / {not Path(record.path).exists()} / {bool(kept)}",
            )
        )
    return rows


def bench_vibe_failed_turn() -> list[tuple[str, str, str]]:
    sys.path.insert(0, str(REPO_ROOT))
    from tests.support import vibe_store as vibe
    from theater.daemon.observation.completion import _FINISHED_FOR_TERMINAL
    from theater.daemon.rpc.jobs import _JOB_ERROR_MESSAGES
    from theater.harness.builtin.plugins.vibe.observer import VibeObserver

    with tempfile.TemporaryDirectory() as tmp:
        store = vibe.Store(Path(tmp))
        user = vibe.public_message("user-1", "user", "question")
        narration = vibe.public_message("assistant-1", "assistant", "Let me look at the files.")
        store.publish(
            generation=vibe.GEN1,
            snapshot_sequence=0,
            state=vibe.source_state([user], turn_status="in_progress"),
            watermark=1,
        )
        source = VibeObserver(root=store.session_root.parent.parent).open_source(cwd="/tmp/work")
        asyncio.run(source.read())
        source.commit_attachment()
        source.acknowledge_source_checkpoint()
        failed = vibe.source_state([user, narration], turn_status="failed")
        failed["latestTurn"]["error"] = VIBE_FAILED_TURN_ERROR
        store.publish(generation=vibe.GEN2, snapshot_sequence=1, state=failed, watermark=2)
        boundary = [event for event in asyncio.run(source.read()).events if event.turn_end][-1]
    state, code = _FINISHED_FOR_TERMINAL[boundary.turn_terminal]
    return [
        ("B7 vibe failed turn", "job state", str(state)),
        ("B7 vibe failed turn", "error_code", str(code)),
        ("B7 vibe failed turn", "result text", repr(boundary.text)),
        ("B7 vibe failed turn", "await error message", str(code in _JOB_ERROR_MESSAGES)),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = bench_cleanup() + bench_vibe_failed_turn()
    if args.json:
        print(
            json.dumps([dict(zip(("item", "metric", "value"), row, strict=True)) for row in rows])
        )
        return 0
    print(f"theater at {REPO_ROOT}")
    for item, metric, value in rows:
        print(f"{item:<40} {metric:<44} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
