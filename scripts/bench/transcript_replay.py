"""Measure Claude transcript replay caused by in-place rewrites of the trailing cost-state record.

Drives the real ``TranscriptSource`` with the shipped Claude observer over temp copies only.
"Before" is this same script run against the base commit's checkout (see --help).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.channels import filesystem
from theater.harness.source import TranscriptSource

# A watcher would gate polls on kqueue timing; every poll must check the file for real here.
filesystem.kqueue_available = lambda: False


def _record(i: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-06-24T18:26:15.348Z",
            "message": {
                "id": f"m-{i}",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": f"record {i}"}],
            },
        }
    )


def _cost_state(duration: int) -> str:
    return json.dumps({"type": "cost-state", "sessionId": "bench", "totalDuration": duration})


def _rewrite_tail_in_place(path: Path, tail: str, generation: int) -> None:
    """Replace the last line with a same-length record and bump mtime, as Claude does hourly."""
    data = path.read_bytes()
    start = data.rstrip(b"\n").rfind(b"\n") + 1
    old = data[start:].rstrip(b"\n")
    new = tail.encode()
    new = new[: len(old)] if len(new) >= len(old) else new + b" " * (len(old) - len(new))
    with path.open("r+b") as fh:
        fh.seek(start)
        fh.write(new)
    st = path.stat()
    bump = st.st_mtime_ns + (generation + 1) * 1_000_000_000
    os.utime(path, ns=(st.st_atime_ns, bump))


async def _drain(s: TranscriptSource) -> int:
    count = 0
    while True:
        batch = await s.read()
        count += len(batch.events)
        if not batch.has_more:
            return count


async def _replay(workdir: Path, body: str, rewrites: int) -> tuple[int, float]:
    """Attach to a header-only file, append *body* live, then rewrite its tail *rewrites* times."""
    root = workdir / "projects"
    (root / "-work").mkdir(parents=True)
    path = root / "-work" / "bench.jsonl"
    path.write_text(json.dumps({"type": "system", "cwd": str(workdir)}) + "\n")
    s = TranscriptSource(ClaudeCodeObserver(root=root), cwd=str(workdir), known_location=str(path))
    batch = await s.read()
    assert batch.attached is not None
    s.commit_attachment()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(body)
    started = time.perf_counter()
    events = await _drain(s)
    for generation in range(rewrites):
        _rewrite_tail_in_place(path, _cost_state(10_000 + generation), generation)
        events += await _drain(s)
    await s.aclose()
    return events, time.perf_counter() - started


def _largest_claude_transcript() -> Path | None:
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return None
    candidates = [p for p in root.glob("*/*.jsonl") if p.is_file()]
    return max(candidates, key=lambda p: p.stat().st_size, default=None)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__ + "\nBefore: git worktree add --detach /tmp/base-wpd 4104dca4 && "
        "uv run --project /tmp/base-wpd python scripts/bench/transcript_replay.py"
    )
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--no-real", action="store_true", help="skip the real-transcript copy")
    args = parser.parse_args()

    synthetic = "".join(_record(i) + "\n" for i in range(args.records))
    synthetic += _cost_state(99_999) + "\n"
    print(f"theater: {Path(sys.modules['theater'].__file__).parent}")
    print(f"{'scenario':<34}{'rewrites':>9}{'events':>9}{'x history':>11}{'seconds':>10}")
    for rewrites in (0, 7, 65):
        with tempfile.TemporaryDirectory() as tmp:
            events, seconds = await _replay(Path(tmp), synthetic, rewrites)
        ratio = events / args.records
        print(
            f"{'synthetic ' + str(args.records):<34}{rewrites:>9}{events:>9}{ratio:>11.1f}"
            f"{seconds:>10.3f}"
        )

    real = None if args.no_real else _largest_claude_transcript()
    if real is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "copy.jsonl"
        shutil.copyfile(real, copy)  # never touch the original
        body = copy.read_text(encoding="utf-8", errors="replace")
    if not body.endswith("\n"):
        body += "\n"
    base = None
    for rewrites in (0, 1):
        with tempfile.TemporaryDirectory() as tmp:
            events, seconds = await _replay(Path(tmp), body, rewrites)
        base = events if base is None else base
        ratio = events / base if base else 0.0
        label = f"real {real.stat().st_size // 1024} KiB"
        print(f"{label:<34}{rewrites:>9}{events:>9}{ratio:>11.1f}{seconds:>10.3f}")


if __name__ == "__main__":
    asyncio.run(main())
