"""Git repositories, a source job and deferred-spawn helpers for the handoff tests."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from sqlalchemy import func, select

from theater.daemon.rpc.spawning import _spawn
from theater.daemon.schema import launch_reservations


def run_git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def init_repository(path: Path) -> Path:
    root = path / "repository"
    root.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "T"),
    ):
        run_git(root, *args)
    (root / "README.md").write_text("one\n")
    run_git(root, "add", "README.md")
    run_git(root, "commit", "-m", "one")
    return root


async def end_source(daemon, handle: str = "src#1", *, target: str = "src", **finish) -> None:
    """Finish a source the way the observer does: pin Git off the loop, then publish."""
    pinned = await daemon.jobs.prepare_pins(target)
    try:
        daemon.jobs.finish(handle, **finish)
    finally:
        daemon.jobs.release_pins(pinned)


def commit_file(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    run_git(repo, "add", name)
    run_git(repo, "commit", "-m", name)
    return run_git(repo, "rev-parse", "HEAD")


def start_source(daemon, repo: Path, *, handle: str = "src#1") -> None:
    daemon.registry.create_spawned(harness="codex", cwd=str(repo), pid="src", name="Writer")
    daemon.jobs.create(
        handle=handle, caller_id="cli", target_id="src", kind="send", prompt="build the parser"
    )


async def defer_spawn(daemon, repo: Path, **extra) -> dict:
    params = {
        "harness": "codex",
        "approval": "yolo",
        "cwd": str(repo),
        "prompt": "Take it from here",
        "after": "Writer",
        **extra,
    }
    return await _spawn(daemon, params)


async def launch_all(daemon) -> None:
    await daemon.handoff.settle()
    await asyncio.sleep(0)
    tasks = daemon.operation_service.owned_tasks
    if tasks:
        await asyncio.gather(*tasks)


def launch_row_count(daemon) -> int:
    return daemon.store.conn.execute(select(func.count()).select_from(launch_reservations)).scalar()
