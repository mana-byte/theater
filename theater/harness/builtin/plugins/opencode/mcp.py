"""OpenCode MCP identity snapshots and lookup."""

from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

from theater import paths
from theater.constants.harness import HARNESS_MCP_SERVER_NAME
from theater.harness.normalization.values import trajectory_identifier

from .constants import (
    CORRELATION_PLUGIN_SUFFIX,
    MCP_CATALOG_FILENAME,
    MCP_CATALOG_MAX_BYTES,
    MCP_CATALOG_MAX_SERVERS,
    MCP_CATALOG_MAX_TOOLS,
    MCP_CATALOG_NAME_MAX_BYTES,
    MCP_CATALOG_VERSION,
)

type _Signature = tuple[int, int, int, int]


def plugin_path(config_path: Path) -> Path:
    return config_path.with_suffix(CORRELATION_PLUGIN_SUFFIX)


def catalog_path(participant_id: str, correlation_dir: Path | None = None) -> Path:
    if correlation_dir is not None:
        return correlation_dir / f"{participant_id}.{MCP_CATALOG_FILENAME}"
    return paths.observation_dir("opencode", participant_id) / MCP_CATALOG_FILENAME


def _bounded_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value if len(encoded) <= MCP_CATALOG_NAME_MAX_BYTES else None


def _identity(server: object, tool: object) -> tuple[str, str] | None:
    server_name = _bounded_name(server)
    tool_name = _bounded_name(tool)
    if server_name is None or tool_name is None:
        return None
    bounded_server = trajectory_identifier(server_name, overflow_prefix="mcp-server")
    bounded_tool = trajectory_identifier(tool_name, overflow_prefix="mcp-tool")
    if bounded_server is None or bounded_tool is None:
        return None
    return bounded_server, bounded_tool


def _theater_identity(value: object) -> tuple[str, str] | None:
    prefix = f"{HARNESS_MCP_SERVER_NAME}_"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    return _identity(HARNESS_MCP_SERVER_NAME, value.removeprefix(prefix))


def _catalog_tools(raw: bytes) -> dict[str, tuple[str, str]]:
    document = json.loads(raw)
    if (
        not isinstance(document, dict)
        or type(document.get("version")) is not int
        or document["version"] != MCP_CATALOG_VERSION
    ):
        raise ValueError("unsupported OpenCode MCP catalog")
    servers = document.get("servers")
    tools = document.get("tools")
    if not isinstance(servers, list) or len(servers) > MCP_CATALOG_MAX_SERVERS:
        raise ValueError("invalid OpenCode MCP server catalog")
    if not isinstance(tools, dict) or len(tools) > MCP_CATALOG_MAX_TOOLS:
        raise ValueError("invalid OpenCode MCP tool catalog")
    server_names = {_bounded_name(server) for server in servers}
    if None in server_names:
        raise ValueError("invalid OpenCode MCP server name")
    result: dict[str, tuple[str, str]] = {}
    for key, value in tools.items():
        tool_key = _bounded_name(key)
        if tool_key is None or not isinstance(value, list) or len(value) != 2:
            raise ValueError("invalid OpenCode MCP tool identity")
        identity = _identity(value[0], value[1])
        if identity is None or value[0] not in server_names:
            raise ValueError("invalid OpenCode MCP tool identity")
        result[tool_key] = identity
    return result


class OpenCodeMcpCatalog:
    """Reload one bounded atomically-written native MCP identity snapshot."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._signature: _Signature | None = None
        self._tools: dict[str, tuple[str, str]] = {}
        self._generation = 0
        self._lock = threading.Lock()

    @property
    def generation(self) -> int:
        with self._lock:
            self._refresh_locked()
            return self._generation

    def identity(self, value: object) -> tuple[str, str] | None:
        theater = _theater_identity(value)
        if theater is not None:
            return theater
        if not isinstance(value, str):
            return None
        with self._lock:
            self._refresh_locked()
            return self._tools.get(value)

    def _refresh_locked(self) -> None:
        if self.path is None:
            return
        try:
            current = self.path.lstat()
        except OSError:
            return
        if not stat.S_ISREG(current.st_mode) or current.st_size > MCP_CATALOG_MAX_BYTES:
            return
        signature = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        if signature == self._signature:
            return
        self._signature = signature
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MCP_CATALOG_MAX_BYTES + 1)
            if len(raw) > MCP_CATALOG_MAX_BYTES:
                return
            tools = _catalog_tools(raw)
        except (OSError, UnicodeError, ValueError, TypeError):
            return
        self._tools = tools
        self._generation += 1


__all__ = ["OpenCodeMcpCatalog", "catalog_path", "plugin_path"]
