"""Pure MCP package precedence and duplicate selection."""

from collections import defaultdict
from collections.abc import Iterable

from theater.mcp_plugins.loading import LOCAL, SHIPPED, LoadedMcpPlugin


def select_packages(
    shipped: Iterable[LoadedMcpPlugin], local: Iterable[LoadedMcpPlugin]
) -> tuple[dict[str, LoadedMcpPlugin], tuple[LoadedMcpPlugin, ...]]:
    """Select unique packages per source, with local packages taking precedence."""
    by_source: dict[str, dict[str, list[LoadedMcpPlugin]]] = {
        SHIPPED: defaultdict(list),
        LOCAL: defaultdict(list),
    }
    for plugin in shipped:
        by_source[SHIPPED][plugin.name].append(plugin)
    for plugin in local:
        by_source[LOCAL][plugin.name].append(plugin)

    duplicates: list[LoadedMcpPlugin] = []
    unique: dict[str, dict[str, LoadedMcpPlugin]] = {SHIPPED: {}, LOCAL: {}}
    for source, groups in by_source.items():
        for name, group in groups.items():
            if len(group) != 1:
                duplicates.extend(group)
                continue
            unique[source][name] = group[0]

    selected = dict(unique[SHIPPED])
    selected.update(unique[LOCAL])
    return selected, tuple(duplicates)
