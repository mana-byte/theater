"""Execute one sidecar from a private launch descriptor."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from theater.mcp_plugins.contracts import McpLaunchPlan

_MAX_PLAN_BYTES = 32 * 1024 * 1024


def load_plan(path: Path) -> McpLaunchPlan:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("sidecar launch descriptor must be a private regular file")
        raw = os.read(fd, _MAX_PLAN_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_PLAN_BYTES:
        raise ValueError("sidecar launch descriptor is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("sidecar launch descriptor is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"command", "argv", "env"}:
        raise ValueError("sidecar launch descriptor has an invalid shape")
    return McpLaunchPlan(command=value["command"], argv=value["argv"], env=value["env"])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: python -m theater.mcp_plugins.runner PLAN_FILE")
    plan = load_plan(Path(args[0]))
    os.execvpe(plan.command, [plan.command, *plan.argv], {**os.environ, **plan.env})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
