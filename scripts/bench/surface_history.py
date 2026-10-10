"""Price every commit of a range in MCP-surface bytes: tools/list plus instructions, per commit."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(cmd: list[str], **kw) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def measure(sha: str, harness: str, repo: Path) -> dict:
    """Extract `theater/` at `sha` and measure it with the current mcp_surface script."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = subprocess.run(
            ["git", "archive", sha, "theater"], cwd=repo, check=True, capture_output=True
        ).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
        env = {**os.environ, "PYTHONPATH": tmp}
        script = str(HERE / "mcp_surface.py")
        out = run([sys.executable, script, "--harness", harness, "--json"], cwd=tmp, env=env)
        return json.loads(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("range", help="git revision range, e.g. 4104dca4..HEAD")
    parser.add_argument("--harness", default="vibe")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    base = args.range.split("..")[0]
    shas = run(["git", "rev-list", "--reverse", "--first-parent", args.range], cwd=repo).split()
    rows = [(base, "base", measure(base, args.harness, repo))]
    for sha in shas:
        subject = run(["git", "log", "-1", "--format=%s", sha], cwd=repo).strip()
        rows.append((sha, subject, measure(sha, args.harness, repo)))
    print("| commit | subject | tools | total B | delta B |\n|---|---|---:|---:|---:|")
    previous = rows[0][2]["total_bytes"]
    for sha, subject, result in rows:
        delta = result["total_bytes"] - previous
        previous = result["total_bytes"]
        print(
            f"| {sha[:8]} | {subject[:60]} | {result['tool_count']} | {result['total_bytes']} | "
            f"{delta:+d} |"
        )


if __name__ == "__main__":
    main()
