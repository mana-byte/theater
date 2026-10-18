"""Measure WP-C spawn and participant ergonomics against an in-process daemon.

Each run builds a throwaway THEATER_HOME, a fake terminal provider (tests/support),
synthetic participants, and a non-Git directory of repositories, then records what an
agent would see. ``--base <worktree>`` re-runs this same file under that checkout's
project to measure "before"; nothing touches ~/.theater or the network.

    git worktree add --detach /tmp/base-wpc 4104dca4
    uv run python scripts/bench/spawn_ergonomics.py --base /tmp/base-wpc
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEAD_ROWS = 250
REPO_COUNT = 12
BAD_APPROVALS = ["auto", "YOLO", "never", "full-auto", "edit"]
COLLISIONS = ["Qbench", "Qbench-2", "Pantalone-Dottore-Brigh"]


def _make_repo_parent(root: Path) -> Path:
    parent = root / "Desktop"
    for index in range(REPO_COUNT):
        repo = parent / f"repo-{index:02}"
        repo.mkdir(parents=True)
        if index == REPO_COUNT - 1:
            (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
        else:
            (repo / ".git").mkdir()
        os.utime(repo, (1_000_000 + index, 1_000_000 + index))
    (parent / "notes").mkdir()
    (parent / ".hidden" / ".git").mkdir(parents=True)
    return parent


def _error_text(exc: BaseException) -> tuple[str, str]:
    return getattr(exc, "code", type(exc).__name__), str(exc)


async def _c1(client, home: Path) -> dict:
    """worktree=true from a non-Git parent of 12 repositories (one a linked worktree)."""
    parent = _make_repo_parent(home)
    try:
        await client.call(
            "spawn", harness="vibe", approval="manual", cwd=str(parent), worktree=True
        )
    except Exception as exc:
        code, message = _error_text(exc)
        named = sum(f"repo-{i:02}" in message for i in range(REPO_COUNT))
        return {
            "code": code,
            "message": message,
            "named": named,
            "accounted": named + (REPO_COUNT - named if "more)" in message else 0),
            "repair_chars": len(message.split("is not", 1)[-1]),
        }
    return {"code": "accepted", "message": "", "named": 0, "accounted": 0, "repair_chars": 0}


async def _c3b(client, home: Path) -> list[dict]:
    out = []
    for value in BAD_APPROVALS:
        try:
            await client.call("spawn", harness="vibe", approval=value, cwd=str(home), prompt="x")
            out.append({"value": value, "code": "accepted", "message": ""})
        except Exception as exc:
            code, message = _error_text(exc)
            out.append({"value": value, "code": code, "message": message})
    return out


async def _c5(daemon, tools, session, home: Path) -> list[str]:
    daemon.config.models.update(claude=["opus"])
    daemon.config.reasoning.update(claude=["high"])
    child = await tools.spawn_session(
        session,
        harness="claude",
        approval="manual",
        cwd=str(home),
        prompt="hi",
        model="opus",
        reasoning_effort="high",
    )
    return sorted(k for k in ("model", "reasoning_effort") if k in child)


async def _d2(daemon, tools, session, me_id: str) -> dict:
    from theater.models import Participant, Status

    for index in range(3):
        daemon.store.upsert_participant(
            Participant(
                id=f"kid-{index}",
                harness="codex",
                parent_id=me_id,
                status=Status.IDLE,
                created_at=float(index),
            )
        )
    plain = await tools.whoami(session)
    plain.pop("cwd", None)  # the checkout path differs between the two runs
    listed = await tools.list_participants(session, children_only=True)
    return {
        "whoami_has_children": "children" in plain,
        "whoami_bytes": len(json.dumps(plain)),
        "children_bytes": len(json.dumps(plain.get("children", []))),
        "list_bytes": len(json.dumps(listed)),
    }


async def _c7(daemon, client, tools, session) -> dict:
    from theater.models import Participant, Status

    for index in range(DEAD_ROWS):
        daemon.store.upsert_participant(
            Participant(
                id=f"dead-{index:03}", harness="vibe", status=Status.DEAD, created_at=10.0 + index
            )
        )
    pages: list[list[str]] = []
    cursor = None
    while len(pages) < 10:
        rows = await tools.list_participants(session, include_dead=True, after_id=cursor)
        if not rows:
            break
        pages.append([row["id"] for row in rows])
        cursor = rows[-1].get("next_after_id", rows[-1]["id"])
    first = await tools.list_participants(session, include_dead=True)
    newest, oldest = f"dead-{DEAD_ROWS - 1:03}", "dead-000"
    out = {
        "first_page_has_newest": newest in pages[0],
        "calls_to_newest": next(i + 1 for i, p in enumerate(pages) if newest in p),
        "calls_to_oldest": next(i + 1 for i, p in enumerate(pages) if oldest in p),
        "truncation_signal": any("next_after_id" in row for row in first),
    }
    try:
        capped = await client.call(
            "participants.list", include_dead=True, ids=["dead-000", "dead-001"], limit=1
        )
        out["ids_with_limit"] = f"accepted ({len(capped)} row)"
    except Exception as exc:
        out["ids_with_limit"] = f"rejected ({_error_text(exc)[0]})"
    return out


def _c8(daemon, home: Path) -> list[dict]:
    for name in COLLISIONS:
        owner = daemon.registry.register(harness="vibe", pane=None, cwd=str(home))
        daemon.registry.rename(owner.id, name)
    other = daemon.registry.register(harness="vibe", pane=None, cwd=str(home))
    out = []
    for name in COLLISIONS:
        try:
            daemon.registry.rename(other.id, name)
            out.append({"name": name, "message": "accepted"})
        except Exception as exc:
            out.append({"name": name, "message": _error_text(exc)[1]})
    return out


async def _measure(home: Path) -> dict:
    os.environ["THEATER_HOME"] = str(home)
    os.environ["THEATER_OPENCODE_VERSION"] = "1.18.29"
    os.environ["XDG_DATA_HOME"] = str(home / "xdg-data")
    import pytest

    from tests.support.shipped import pin_installed_vibe_version
    from tests.support.terminal_provider import FakeTerminalProvider
    from theater import harness, paths
    from theater.client import DaemonClient
    from theater.config import Config
    from theater.daemon.server import Daemon
    from theater.mcp import tools

    paths.ensure_home()
    harness.install(Config(), local_dir=Path("/nonexistent/theater-harnesses"))
    patch = pytest.MonkeyPatch()
    binaries = {h.binary for h in harness.HARNESSES.values()}
    real_which = shutil.which
    patch.setattr(
        shutil, "which", lambda c, *a, **k: sys.executable if c in binaries else real_which(c)
    )
    pin_installed_vibe_version(patch, None, binary=sys.executable)

    daemon = Daemon(harnesses={})
    FakeTerminalProvider().install(daemon)
    await daemon.start()
    try:
        async with DaemonClient(autostart=False) as client:
            me = await client.call("hello", harness="vibe", cwd=str(home))
            session = tools.Session(participant_id=me["id"], harness="vibe", client=client)
            await session.identify()
            return {
                "c1": await _c1(client, home),
                "c3b": await _c3b(client, home),
                "c5": await _c5(daemon, tools, session, home),
                "d2": await _d2(daemon, tools, session, me["id"]),
                "c7": await _c7(daemon, client, tools, session),
                "c8": _c8(daemon, home),
            }
    finally:
        await daemon.aclose()
        patch.undo()


def _run_here() -> dict:
    home = Path(tempfile.mkdtemp(prefix="thb-", dir="/tmp"))
    try:
        return asyncio.run(_measure(home))
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _run_base(base: Path) -> dict:
    proc = subprocess.run(
        ["uv", "run", "--quiet", "--project", str(base), "python", __file__, "--emit-json"],
        cwd=base,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _clip(text: str, width: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _table(before: dict, after: dict) -> list[tuple[str, str, str, str]]:
    rows = []
    b1, a1 = before["c1"], after["c1"]
    rows.append(("C1", "error code", b1["code"], a1["code"]))
    rows.append(
        (
            "C1",
            "repos named / accounted (of 12)",
            f"{b1['named']}/{b1['accounted']}",
            f"{a1['named']}/{a1['accounted']}",
        )
    )
    rows.append(
        ("C1", "repair chars after 'is not'", str(b1["repair_chars"]), str(a1["repair_chars"]))
    )
    rows.append(("C1", "message", _clip(b1["message"]), _clip(a1["message"])))
    for b3, a3 in zip(before["c3b"], after["c3b"], strict=True):
        rows.append(
            (
                "C3b",
                f"approval={b3['value']!r} (code, chars)",
                f"{b3['code']}, {len(b3['message'])}: {_clip(b3['message'], 60)}",
                f"{a3['code']}, {len(a3['message'])}: {_clip(a3['message'], 60)}",
            )
        )
    rows.append(
        (
            "C5",
            "spawn result echoes",
            ",".join(before["c5"]) or "none",
            ",".join(after["c5"]) or "none",
        )
    )
    b7, a7 = before["c7"], after["c7"]
    rows.append(
        (
            "C7",
            f"newest of {DEAD_ROWS} dead on page 1",
            str(b7["first_page_has_newest"]),
            str(a7["first_page_has_newest"]),
        )
    )
    rows.append(
        (
            "C7",
            "calls to reach newest dead row",
            str(b7["calls_to_newest"]),
            str(a7["calls_to_newest"]),
        )
    )
    rows.append(
        (
            "C7",
            "calls to reach oldest dead row",
            str(b7["calls_to_oldest"]),
            str(a7["calls_to_oldest"]),
        )
    )
    rows.append(
        (
            "C7",
            "full page carries next_after_id",
            str(b7["truncation_signal"]),
            str(a7["truncation_signal"]),
        )
    )
    rows.append(("C7", "ids + limit", b7["ids_with_limit"], a7["ids_with_limit"]))
    for b8, a8 in zip(before["c8"], after["c8"], strict=True):
        rows.append(
            ("C8", f"NameTaken {b8['name']!r}", _clip(b8["message"], 70), _clip(a8["message"], 70))
        )
    bd, ad = before["d2"], after["d2"]
    rows.append(
        (
            "D2",
            "calls to learn children status",
            "1" if bd["whoami_has_children"] else "2",
            "1" if ad["whoami_has_children"] else "2",
        )
    )
    rows.append(
        (
            "D2",
            "bytes to learn 3 children",
            str(bd["whoami_bytes"] + bd["list_bytes"]),
            str(ad["whoami_bytes"]),
        )
    )
    rows.append(
        (
            "D2",
            "whoami bytes excl. cwd (3 children)",
            str(bd["whoami_bytes"]),
            str(ad["whoami_bytes"]),
        )
    )
    rows.append(("D2", "children bytes added to whoami", "0", str(ad["children_bytes"])))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", type=Path, help="checkout of the base commit to measure")
    parser.add_argument("--emit-json", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    here = _run_here()
    if args.emit_json:
        print(json.dumps(here))
        return 0
    if args.base is None:
        print(json.dumps(here, indent=2))
        return 0
    before = _run_base(args.base)
    for item, metric, old, new in _table(before, here):
        print(f"{item:4} | {metric:34} | before: {old}")
        print(f"{'':4} | {'':34} | after:  {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
