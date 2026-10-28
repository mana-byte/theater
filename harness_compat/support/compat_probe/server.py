"""Stdio MCP server for the compat-suite probe: its one tool leaves a marker file behind."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from mcp.server import MCPServer

_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def build(marker_dir: Path) -> MCPServer:
    server = MCPServer("compat_probe")

    @server.tool()
    def compat_probe_mark(label: str) -> str:
        """Record that this tool ran by writing a marker file named after the label."""
        if _LABEL.fullmatch(label) is None:
            raise ValueError("label must be 1-64 characters of letters, digits, '_', '.' or '-'")
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / label).write_text("marked\n", encoding="utf-8")
        return f"marked {label}"

    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker-dir", required=True, type=Path)
    build(parser.parse_args().marker_dir).run()


if __name__ == "__main__":
    main()
