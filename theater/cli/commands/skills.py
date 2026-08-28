"""Human-facing declarative skill discovery."""

from __future__ import annotations

import asyncio
import json

from theater.client import DaemonClient


def _skills_snapshot() -> dict:
    async def go():
        async with DaemonClient(autostart=False) as client:
            return await client.call("skills.list")

    try:
        data = asyncio.run(go())
    except (FileNotFoundError, ConnectionRefusedError, ConnectionError, OSError) as exc:
        raise ConnectionError("no daemon running; start it with `theater daemon`") from exc
    assert isinstance(data, dict)
    return data


def cmd_skills(args) -> int:
    data = _skills_snapshot()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    skills = data.get("skills") or []
    if skills:
        width = max(len(str(skill.get("name", ""))) for skill in skills)
        print(f"{'NAME':<{width}}  {'SOURCE':<8}  DESCRIPTION")
        for skill in skills:
            print(
                f"{skill.get('name', '-'):<{width}}  {skill.get('source', '-'):<8}  "
                f"{skill.get('description', '-') }"
            )
    else:
        print("no skills available")

    rejections = data.get("rejections") or []
    if rejections:
        print("\nrejected user skill packages:")
        for rejection in rejections:
            name = rejection.get("name") or "unnamed"
            path = rejection.get("path") or "unknown path"
            print(f"- {name} ({path}): {rejection.get('error', 'rejected')}")
    return 0
