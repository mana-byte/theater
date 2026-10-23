"""Wire adapters for daemon-owned skill discovery and loading."""

from __future__ import annotations

from theater.daemon import skill_catalog
from theater.daemon.rpc.router import method


@method("skills.list")
async def _skills_list(daemon, params: dict) -> dict:
    return skill_catalog.list_skills(daemon, play=params.get("play"), grouped=params.get("grouped"))


@method("skills.load")
async def _skills_load(daemon, params: dict) -> dict:
    return skill_catalog.load_skill(
        daemon,
        params.get("name"),
        preview=params.get("preview", False),
        caller_id=params.get("caller_id"),
    )
