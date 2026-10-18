"""Generic required-plugin admission for pinned plays."""

from __future__ import annotations

from theater.daemon.plays.models import PlayRefused
from theater.mcp_plugins import registry as mcp_registry


def require_plugins(rules, *, store=None, participant_id=None, connection=None) -> None:
    """Validate availability and grants; existing participants also need an attachment."""
    snapshot = mcp_registry.catalog()
    for name in rules.plugins:
        try:
            plugin = snapshot.get(name)
        except LookupError as exc:
            raise PlayRefused(
                f"play {rules.play!r} requires enabled MCP plugin {name!r}; "
                "enable it in [mcp].enabled and restart Theater before starting this play"
            ) from exc
        declared = rules.plugin_permissions.get(name, {})
        unknown = {
            grant for grants in declared.values() for grant in grants
        } - plugin.play_permissions
        if unknown:
            raise PlayRefused(
                f"play {rules.play!r} declares unknown {name!r} grants: {sorted(unknown)}"
            )
        if participant_id is not None:
            from sqlalchemy import select

            from theater.daemon.schema import participant_mcp_plugins

            conn = store.conn if connection is None else connection
            attached = conn.execute(
                select(participant_mcp_plugins.c.plugin_name).where(
                    participant_mcp_plugins.c.participant_id == participant_id,
                    participant_mcp_plugins.c.plugin_name == name,
                )
            ).first()
            if attached is None:
                raise PlayRefused(
                    f"play {rules.play!r} requires MCP plugin {name!r} attached to this "
                    "participant; start a new session with the plugin enabled"
                )


def require_rendered_plugins(participant_id, *, store, rendered) -> None:
    """A dependent spawn must not lose a required sidecar during rendering."""
    held = store.plays.active_membership(participant_id)
    if held is None:
        return
    require_plugins(held.rules)
    missing = set(held.rules.plugins) - set(rendered)
    if missing:
        raise PlayRefused(
            f"play {held.play!r} could not attach required MCP plugins: {sorted(missing)}; "
            "inspect theater plugins and retry with a supported harness"
        )
