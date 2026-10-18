"""The harness list RPC and harness-name normalisation on hello."""

from __future__ import annotations

from theater import harness as harness_registry
from theater.harness import HARNESSES
from theater.harness.contracts.runtime import RuntimeCompatibility


async def test_hello_normalizes_claude_code_to_claude(client):
    """A misreported harness name must not silently become unobservable."""
    me = await client.call("hello", harness="claude_code", cwd="/tmp")
    assert me["harness"] == "claude"


async def test_unknown_harness_name_passes_through(client, terminal_provider):
    """A genuinely unknown harness is not rejected — just unobservable."""
    me = await client.call("hello", harness="cursor", cwd="/tmp")
    assert me["harness"] == "cursor"


async def test_harnesses_lists_what_the_daemon_can_spawn(client):
    rows = await client.call("harnesses")
    names = {r["name"] for r in rows}
    assert names == set(HARNESSES)
    assert all(r["icon"] and r["binary"] for r in rows)


async def test_harnesses_reports_install_state(client, monkeypatch):
    """The daemon's PATH is the one that matters: it runs the binary."""
    monkeypatch.setattr(harness_registry.shutil, "which", lambda binary: None)
    rows = await client.call("harnesses")
    assert all(r["installed"] is False and r["path"] is None for r in rows)


async def test_harnesses_is_sorted_so_callers_need_not_re_sort(client):
    rows = await client.call("harnesses")
    assert [r["name"] for r in rows] == sorted(r["name"] for r in rows)


async def test_harnesses_reports_daemon_native_compatibility(client, daemon, monkeypatch):
    monkeypatch.setattr(harness_registry.shutil, "which", lambda binary: f"/bin/{binary}")

    async def probe(name, callback, context, *, configuration):
        del name, context, configuration
        if callback.__name__ == "probe_claude_native_compatibility":
            return RuntimeCompatibility(
                supported=False,
                policy="claude-messaging-native-controls-2.1.248",
                native_version="2.1.220",
                reason="below floor",
            )
        return RuntimeCompatibility(
            supported=True,
            policy="qualified-test",
            native_version="1.0.0",
        )

    monkeypatch.setattr(daemon.compatibility_probes, "probe", probe)
    rows = {row["name"]: row for row in await client.call("harnesses")}

    assert rows["claude"]["native_compatibility"]["status"] == "legacy-only"
    assert rows["claude"]["native_compatibility"]["qualified_range"] is None
    assert rows["claude"]["native_compatibility"]["reason"] == (
        "no native runtime; controls use the provider terminal"
    )
    assert rows["codex"]["native_compatibility"]["status"] == "native-compatible"
    assert rows["codex"]["native_compatibility"]["wiring"] == "native"
    assert rows["opencode"]["native_compatibility"]["status"] == "native-compatible"
    assert rows["pi"]["native_compatibility"]["status"] == "native-compatible"
    assert rows["vibe"]["native_compatibility"]["status"] == "legacy-only"
