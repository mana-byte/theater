"""JSON-only compatibility gateway for MCP-plugin sidecars."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from theater.plugin_client import (
    PluginAuthenticationError,
    PluginCapabilityError,
    PluginRemoteError,
    TheaterPluginClient,
)
from theater.protocol import MAX_MESSAGE_BYTES


def cmd_plugin_call(args) -> int:
    """Read one JSON object from stdin and emit one deterministic JSON envelope."""
    try:
        params = _read_params()
        result = asyncio.run(_call(args.operation, params, args.credential_file))
    except PluginCapabilityError as exc:
        return _emit_error(
            "capability_denied",
            str(exc),
            {"required": exc.required, "granted": list(exc.granted)},
        )
    except PluginAuthenticationError as exc:
        return _emit_error("plugin_auth_failed", str(exc))
    except PluginRemoteError as exc:
        return _emit_error(exc.code, exc.message, exc.details)
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        return _emit_error("bad_request", str(exc))
    except Exception as exc:
        return _emit_error("internal", f"{type(exc).__name__}: {exc}")
    _emit({"ok": True, "result": result})
    return 0


async def _call(operation: str, params: dict[str, Any], credential_file: str | None) -> Any:
    async with TheaterPluginClient(credential_path=credential_file, autostart=False) as client:
        return await client.call(operation, params)


def _read_params() -> dict[str, Any]:
    stream = getattr(sys.stdin, "buffer", None)
    raw = (
        stream.read(MAX_MESSAGE_BYTES + 1)
        if stream is not None
        else sys.stdin.read(MAX_MESSAGE_BYTES + 1).encode("utf-8")
    )
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError(f"plugin call input exceeds {MAX_MESSAGE_BYTES} bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"plugin call input must be one JSON object: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise TypeError("plugin call input must be one JSON object")
    return value


def _emit_error(code: str, message: str, details: dict[str, Any] | None = None) -> int:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    _emit({"ok": False, "error": error})
    return 1


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


__all__ = ["cmd_plugin_call"]
