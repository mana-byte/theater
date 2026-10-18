"""Test-only helpers for the Codex native runtime proof.

Nothing in this package is production code. It exists so
``tests/integration/codex/test_native_smoke.py`` can speak the real installed
Codex app-server protocol (WebSocket over a Unix socket, JSON-RPC-shaped
messages without a jsonrpc version field) without adding dependencies.
"""
