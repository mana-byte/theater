"""Measure the MCP surface every agent pays for: bytes of tools/list plus server instructions."""

from __future__ import annotations

import argparse
import asyncio
import json

from theater.mcp.server import build

TOOLSETS = ("control", "wait")


async def measure(harness: str) -> dict:
    """Wire bytes per tool and per server, as an agent of `harness` receives them."""
    servers = {}
    for toolset in TOOLSETS:
        server = build("bench", harness, toolset)
        tools = await server.list_tools()
        per_tool = {
            t.name: len(json.dumps(t.model_dump(mode="json", exclude_none=True)).encode())
            for t in tools
        }
        servers[toolset] = {
            "instructions": len((server.instructions or "").encode()),
            "tools": per_tool,
            "tools_total": sum(per_tool.values()),
        }
    total = sum(s["instructions"] + s["tools_total"] for s in servers.values())
    count = sum(len(s["tools"]) for s in servers.values())
    return {"harness": harness, "tool_count": count, "total_bytes": total, "servers": servers}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", default="vibe")
    parser.add_argument("--json", action="store_true", help="print the full breakdown as JSON")
    args = parser.parse_args()
    result = await measure(args.harness)
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True))
        return
    print(
        f"harness={result['harness']} tools={result['tool_count']} total={result['total_bytes']} B"
    )
    for toolset, server in result["servers"].items():
        print(
            f"  [{toolset}] instructions={server['instructions']} B tools={server['tools_total']} B"
        )
        for name, size in sorted(server["tools"].items(), key=lambda kv: -kv[1]):
            print(f"    {size:6d}  {name}")


if __name__ == "__main__":
    asyncio.run(main())
