"""Human-facing declarative skill discovery."""

from __future__ import annotations

import asyncio
import json

from theater.client import DaemonClient
from theater.protocol import RemoteError


def _skills_snapshot() -> dict:
    async def go():
        async with DaemonClient(autostart=False) as client:
            return await client.call("skills.list")

    try:
        data = asyncio.run(go())
    except (FileNotFoundError, ConnectionRefusedError, ConnectionError, OSError) as exc:
        raise ConnectionError("no daemon running; start it with `theater daemon`") from exc
    except RemoteError as exc:
        if exc.code == "unknown_method":
            raise ConnectionError(
                "running daemon lacks skills support; run `theater restart`"
            ) from exc
        raise
    assert isinstance(data, dict)
    return data


def _terminal_line(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    return "".join(char if char.isprintable() else " " for char in str(value))


def cmd_skills(args) -> int:
    data = _skills_snapshot()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    skills = data.get("skills") or []
    if skills:
        rows = [
            (
                _terminal_line(skill.get("name"), "-"),
                _terminal_line(skill.get("source"), "-"),
                _terminal_line(skill.get("description"), "-"),
            )
            for skill in skills
        ]
        width = max(len(name) for name, _, _ in rows)
        print(f"{'NAME':<{width}}  {'SOURCE':<8}  DESCRIPTION")
        for name, source, description in rows:
            print(f"{name:<{width}}  {source:<8}  {description}")
    else:
        print("no skills available")

    rejections = data.get("rejections") or []
    if rejections:
        print("\nrejected user skill packages:")
        for rejection in rejections:
            name = _terminal_line(rejection.get("name"), "unnamed")
            path = _terminal_line(rejection.get("path"), "unknown path")
            error = _terminal_line(rejection.get("error"), "rejected")
            print(f"- {name} ({path}): {error}")
    return 0
