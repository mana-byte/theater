"""Authenticated play facts for plugin-owned policy and retention."""

from __future__ import annotations

from sqlalchemy import select

from theater.daemon.schema import play_members
from theater.mcp_plugins import registry as mcp_registry
from theater.models import BadRequest, Status


async def context(daemon, _params, record) -> dict:
    """Only credential identity and actual membership select a play."""
    held = daemon.plays.membership(record.participant_id)
    enabled = any(plugin.name == record.plugin_name for plugin in mcp_registry.catalog().servers)
    play = None
    if held is not None:
        play = {
            "instance_id": held.instance_id,
            "master_id": held.master_id,
            "name": held.play,
            "position": held.position,
            "plugins": list(held.rules.plugins),
            "permissions": list(
                held.rules.plugin_permissions.get(record.plugin_name, {}).get(held.position, ())
            ),
        }
    return {
        "participant_id": record.participant_id,
        "plugin_name": record.plugin_name,
        "enabled": enabled,
        "play": play,
    }


async def members(daemon, _params, record) -> list[dict]:
    held = daemon.plays.membership(record.participant_id)
    if held is None or record.plugin_name not in held.rules.plugins:
        return []
    rows = daemon.store.conn.execute(
        select(
            play_members.c.participant_id,
            play_members.c.position,
        ).where(play_members.c.instance_id == held.instance_id)
    ).all()
    result = []
    for participant_id, position in rows:
        participant = daemon.store.get_participant(participant_id)
        if participant is not None and participant.status is not Status.DEAD:
            result.append(
                {"participant_id": participant_id, "name": participant.name, "position": position}
            )
    return result


async def instances_status(daemon, params, record) -> list[dict]:
    ids = params.get("instance_ids")
    if (
        not isinstance(ids, list)
        or len(ids) > 100
        or any(not isinstance(value, str) or not value or len(value) > 128 for value in ids)
    ):
        raise BadRequest("plays.instances_status requires at most 100 nonempty instance IDs")
    result = []
    for instance_id in ids:
        rules = daemon.store.plays.rules(instance_id)
        if rules is not None and record.plugin_name not in rules.plugins:
            raise BadRequest("plugin cannot inspect an instance without its pinned dependency")
        result.append(
            {
                "instance_id": instance_id,
                "active": daemon.store.plays.live_master(instance_id) is not None,
            }
        )
    return result
