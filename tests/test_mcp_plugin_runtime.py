"""Focused runtime coverage for participant-scoped MCP-plugin sidecars."""

from __future__ import annotations

import asyncio
import io
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from theater import paths
from theater.cli.commands import plugin as plugin_cli
from theater.client import CALL_TIMEOUT, DaemonClient
from theater.constants.plugins import MCP_PLUGIN_SPAWN_OMISSION_MAX
from theater.daemon.persistence.store import Store
from theater.daemon.plugins.credentials import mint_credential
from theater.daemon.plugins.dispatch import OPERATIONS, authenticate
from theater.daemon.registry import Registry
from theater.daemon.schema import participant_mcp_plugins
from theater.daemon.spawning import planning
from theater.daemon.spawning.models import SpawnRequest
from theater.harness.base import LaunchPlan
from theater.mcp_plugins import (
    MCP_PLUGIN_API_VERSION,
    CompiledMcpPlugin,
    McpLaunchManifest,
    McpLaunchPlan,
    PluginCapability,
)
from theater.mcp_plugins import registry as mcp_registry
from theater.mcp_plugins.registry import McpPluginDiagnostic
from theater.models import Status
from theater.plugin_client import PluginCapabilityError, TheaterPluginClient
from theater.protocol import RemoteError


def _plugin(
    *,
    name: str = "acme",
    capabilities: frozenset[PluginCapability] = frozenset({PluginCapability.PARTICIPANTS_READ}),
    planner=None,
) -> CompiledMcpPlugin:
    if planner is None:

        def planner(_context) -> McpLaunchPlan:
            return McpLaunchPlan(command="acme-server")

    return CompiledMcpPlugin(
        name=name,
        description="test MCP sidecar",
        capabilities=capabilities,
        config={},
        launch=McpLaunchManifest(planner=planner),
    )


@pytest.fixture
def isolated_mcp_registry():
    prior = dict(mcp_registry.MCP_SERVERS)
    prior_diagnostics = list(mcp_registry._DIAGNOSTICS)
    mcp_registry.MCP_SERVERS.clear()
    mcp_registry._DIAGNOSTICS.clear()
    try:
        yield mcp_registry.MCP_SERVERS
    finally:
        mcp_registry.MCP_SERVERS.clear()
        mcp_registry.MCP_SERVERS.update(prior)
        mcp_registry._DIAGNOSTICS.clear()
        mcp_registry._DIAGNOSTICS.extend(prior_diagnostics)


@pytest.fixture
def rendering_sidecars(monkeypatch):
    monkeypatch.setattr(planning, "supports_mcp_rendering", lambda _harness: True)


def _request() -> SpawnRequest:
    return SpawnRequest(harness="fake", prompt="", cwd="/tmp", approval="manual")


def _write_credential(path: Path, credential: str) -> None:
    planning.write_plan_files(LaunchPlan(argv=[], private_files={path: credential + "\n"}))


def _attach_credential(
    store,
    participant_id: str,
    *,
    grants: frozenset[PluginCapability],
    name: str = "acme",
):
    material = mint_credential()
    credential_path = paths.participant_mcp_plugin_dir(participant_id, name) / "credential"
    _write_credential(credential_path, material.credential)
    store.set_mcp_plugin_credential(
        participant_id,
        plugin_name=name,
        api_version=MCP_PLUGIN_API_VERSION,
        credential_id=material.credential_id,
        credential_verifier=material.verifier,
        grants=grants,
        credential_path=str(credential_path),
    )
    return material, credential_path


def test_sidecar_planning_materializes_confined_artifacts_and_a_0600_credential(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    def sidecar_plan(_context) -> McpLaunchPlan:
        return McpLaunchPlan(
            command="acme-server",
            argv=("--stdio",),
            env={"ACME_MODE": "test"},
            files={Path("public/config.json"): "{}"},
            private_files={Path("private/config.secret"): "secret"},
        )

    isolated_mcp_registry["acme"] = _plugin(
        capabilities=frozenset({PluginCapability.PARTICIPANTS_READ}),
        planner=sidecar_plan,
    )
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    observed: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        observed.append(mcp_servers)
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    plan = planning.build_plan(_request(), participant, None, registry=registry)

    assert len(observed) == 1
    assert [item.name for item in observed[0]] == ["theater", "theater_wait", "acme"]
    spec = observed[0][-1]
    assert spec.name == "acme"
    assert spec.args == ("--stdio",)
    credential_path = Path(spec.env["THEATER_PLUGIN_CREDENTIAL_PATH"])
    root = paths.participant_mcp_plugin_dir(participant.id, "acme")
    assert credential_path.parent == root
    assert set(plan.files) == {root / "public/config.json"}
    assert root / "private/config.secret" in plan.private_files
    assert credential_path in plan.private_files

    planning.record_plan_artifacts(participant, plan, registry)
    planning.write_plan_files(plan)
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(credential_path.parent.stat().st_mode) == 0o700

    (record,) = registry.store.mcp_plugin_credentials(participant.id)
    assert record.plugin_name == "acme"
    assert record.api_version == MCP_PLUGIN_API_VERSION
    assert record.grants == frozenset({PluginCapability.PARTICIPANTS_READ})
    row = registry.store.conn.execute(participant_mcp_plugins.select()).first()
    assert row is not None
    assert credential_path.read_text().strip() not in json.dumps(dict(row._mapping))


def test_symlinked_or_broken_sidecars_are_omitted_without_failing_the_harness_plan(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars, tmp_path
):
    good = _plugin(name="good")

    def bad_plan(context) -> McpLaunchPlan:
        plugin_root = paths.participant_mcp_plugin_dir(context.participant_id, "bad")
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / "nested").symlink_to(tmp_path / "outside")
        return McpLaunchPlan(command="bad-server", files={Path("nested/config"): "x"})

    isolated_mcp_registry["bad"] = _plugin(name="bad", planner=bad_plan)
    isolated_mcp_registry["good"] = good
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    seen: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        seen.append(mcp_servers)
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    plan = planning.build_plan(_request(), participant, None, registry=registry)

    assert plan.argv == ["fake"]
    assert [item.name for item in seen[0]] == ["theater", "theater_wait", "good"]
    attached = registry.store.mcp_plugin_credentials(participant.id)
    assert [item.plugin_name for item in attached] == ["good"]
    events = registry.store.bus_tail(limit=20)
    assert any(
        event["kind"] == "mcp_plugin.omitted" and event["payload"]["plugin"] == "bad"
        for event in events
    )


def test_sidecar_artifact_collisions_are_omitted_without_overwriting_harness_files(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    def sidecar_plan(_context) -> McpLaunchPlan:
        return McpLaunchPlan(command="acme-server", files={Path("config"): "sidecar"})

    isolated_mcp_registry["acme"] = _plugin(planner=sidecar_plan)
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    root = paths.participant_mcp_plugin_dir(participant.id, "acme")
    observed: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        observed.append(mcp_servers)
        return LaunchPlan(argv=["fake"], files={root / "config" / "harness.json": "{}"})

    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    plan = planning.build_plan(_request(), participant, None, registry=registry)

    assert [item.name for item in observed[0]] == ["theater", "theater_wait", "acme"]
    assert [item.name for item in observed[1]] == ["theater", "theater_wait"]
    assert plan.files == {root / "config" / "harness.json": "{}"}
    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert root / ".theater-plugin-credential" not in plan.private_files


def test_sidecar_persistence_failure_rolls_back_its_record_and_empty_root(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    isolated_mcp_registry["acme"] = _plugin()
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    root = paths.participant_mcp_plugin_dir(participant.id, "acme")
    persist = registry.store.set_mcp_plugin_credential

    def persist_then_fail(*args, **kwargs) -> None:
        persist(*args, **kwargs)
        raise RuntimeError("simulated persistence failure")

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        assert [item.name for item in mcp_servers] == ["theater", "theater_wait"]
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(registry.store, "set_mcp_plugin_credential", persist_then_fail)
    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    assert planning.build_plan(_request(), participant, None, registry=registry).argv == ["fake"]

    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert not root.exists()


def test_harness_rejection_of_sidecars_omits_them_without_failing_the_spawn_plan(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    isolated_mcp_registry["acme"] = _plugin()
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    attempts: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        attempts.append(mcp_servers)
        if any(server.name == "acme" for server in mcp_servers):
            raise ValueError("renderer cannot use this sidecar")
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    assert planning.build_plan(_request(), participant, None, registry=registry).argv == ["fake"]

    assert [item.name for item in attempts[0]] == ["theater", "theater_wait", "acme"]
    assert [item.name for item in attempts[1]] == ["theater", "theater_wait"]
    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert any(
        event["kind"] == "mcp_plugin.omitted" and event["payload"]["plugin"] == "acme"
        for event in registry.store.bus_tail(limit=20)
    )


def test_build_plan_renders_core_servers_with_empty_and_configured_plugin_sets(
    registry, isolated_mcp_registry
):
    request = SpawnRequest(harness="codex", prompt="", cwd="/tmp", approval="manual")
    empty = registry.create_spawned(harness="codex", cwd="/tmp")

    empty_plan = planning.build_plan(request, empty, None, registry=registry)
    assert _rendered_codex_server_names(empty_plan) == {"theater", "theater_wait"}

    isolated_mcp_registry["acme"] = _plugin()
    configured = registry.create_spawned(harness="codex", cwd="/tmp")

    configured_plan = planning.build_plan(request, configured, None, registry=registry)
    assert _rendered_codex_server_names(configured_plan) == {"acme", "theater", "theater_wait"}


def test_unrenderable_harness_omits_sidecars_without_persisting_credentials(
    monkeypatch, registry, isolated_mcp_registry
):
    isolated_mcp_registry["acme"] = _plugin()
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    observed: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        observed.append(mcp_servers)
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "supports_mcp_rendering", lambda _harness: False)
    monkeypatch.setattr(planning, "plan_launch", harness_plan)

    assert planning.build_plan(_request(), participant, None, registry=registry).argv == ["fake"]
    assert [item.name for item in observed[0]] == ["theater", "theater_wait"]
    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert any(
        event["kind"] == "mcp_plugin.omitted"
        and event["payload"]
        == {
            "plugin": "acme",
            "stage": "rendering",
            "error": (
                "ValueError: the selected harness does not render generic MCP server specifications"
            ),
        }
        for event in registry.store.bus_tail(limit=20)
    )


@pytest.mark.parametrize("name", ("theater", "theater_wait"))
def test_reserved_core_server_names_are_omitted_without_shadowing_core_specs(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars, name
):
    isolated_mcp_registry[name] = _plugin(name=name)
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    observed: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        observed.append(mcp_servers)
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "plan_launch", harness_plan)

    assert planning.build_plan(_request(), participant, None, registry=registry).argv == ["fake"]
    assert [item.name for item in observed[0]] == ["theater", "theater_wait"]
    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert any(
        event["kind"] == "mcp_plugin.omitted"
        and event["payload"]["plugin"] == name
        and "reserved by Theater" in event["payload"]["error"]
        for event in registry.store.bus_tail(limit=20)
    )


def test_registry_diagnostics_emit_bounded_safe_spawn_omissions(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    for index in range(MCP_PLUGIN_SPAWN_OMISSION_MAX + 1):
        mcp_registry._DIAGNOSTICS.append(
            McpPluginDiagnostic(name=f"broken{index}", error=f"secret-{index}")
        )
    participant = registry.create_spawned(harness="fake", cwd="/tmp")

    monkeypatch.setattr(
        planning,
        "plan_launch",
        lambda _harness, **_kwargs: LaunchPlan(argv=["fake"]),
    )

    assert planning.build_plan(_request(), participant, None, registry=registry).argv == ["fake"]
    omissions = [
        event
        for event in registry.store.bus_tail(limit=MCP_PLUGIN_SPAWN_OMISSION_MAX + 5)
        if event["kind"] == "mcp_plugin.omitted" and event["payload"]["stage"] == "registry"
    ]
    assert len(omissions) == MCP_PLUGIN_SPAWN_OMISSION_MAX
    assert all("secret-" not in event["payload"]["error"] for event in omissions)


def _rendered_codex_server_names(plan: LaunchPlan) -> set[str]:
    return {
        argument.removeprefix("mcp_servers.").split(".", 1)[0]
        for argument in plan.argv
        if argument.startswith("mcp_servers.") and ".command=" in argument
    }


async def test_plugin_authentication_happens_on_every_call_and_denials_are_structured(
    daemon, client
):
    actor = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    credential, _path = _attach_credential(
        daemon.store,
        actor.id,
        grants=frozenset({PluginCapability.PARTICIPANTS_READ}),
    )

    with pytest.raises(RemoteError) as malformed:
        await client.call(
            "plugin.call",
            credential="not-a-plugin-credential",
            operation="participants.get",
            params={"id": actor.id},
        )
    assert malformed.value.code == "plugin_auth_failed"

    result = await client.call(
        "plugin.call",
        credential=credential.credential,
        operation="participants.get",
        params={"id": actor.id},
    )
    assert result["id"] == actor.id

    with pytest.raises(RemoteError) as denied:
        await client.call(
            "plugin.call",
            credential=credential.credential,
            operation="sessions.send",
            params={"target": actor.id, "prompt": "nope"},
        )
    assert denied.value.code == "capability_denied"
    assert denied.value.details == {
        "required": "sessions.send",
        "granted": ["participants.read"],
    }

    daemon.store.delete_mcp_plugin_credentials(actor.id)
    with pytest.raises(RemoteError) as revoked:
        await client.call(
            "plugin.call",
            credential=credential.credential,
            operation="participants.get",
            params={"id": actor.id},
        )
    assert revoked.value.code == "plugin_auth_failed"


async def test_plugin_identity_is_credential_owned_and_send_keeps_busy_protection(
    daemon, client, fake_tmux
):
    actor = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    credential, _path = _attach_credential(
        daemon.store,
        actor.id,
        grants=frozenset({PluginCapability.SESSIONS_SEND}),
    )
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    trusted = daemon.registry.get(target["id"])
    trusted.session_id = "trusted-target"
    trusted.session_correlation = "operator"
    daemon.store.upsert_participant(trusted)
    daemon.registry.set_status(target["id"], Status.WORKING)

    with pytest.raises(RemoteError) as busy:
        await client.call(
            "plugin.call",
            credential=credential.credential,
            operation="sessions.send",
            params={"target": target["id"], "prompt": "work", "caller_id": "spoofed"},
        )
    assert busy.value.code == "busy"

    daemon.registry.set_status(target["id"], Status.IDLE)
    job = await client.call(
        "plugin.call",
        credential=credential.credential,
        operation="sessions.send",
        params={"target": target["id"], "prompt": "work", "caller_id": "spoofed"},
    )
    assert job["caller_id"] == actor.id
    assert fake_tmux.sent[-1] == ("%1", "work")


async def test_plugin_preserves_parent_filters_but_forces_spawn_parent_identity(daemon, client):
    actor = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="fake", cwd="/tmp", parent_id=actor.id)
    daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    credential, credential_path = _attach_credential(
        daemon.store,
        actor.id,
        grants=frozenset({PluginCapability.PARTICIPANTS_READ, PluginCapability.SESSIONS_SPAWN}),
    )
    typed = TheaterPluginClient(credential_path=credential_path, client=client)

    rows = await typed.list_participants(parent_id=actor.id)
    assert [row["id"] for row in rows] == [child.id]

    spawned = await client.call(
        "plugin.call",
        credential=credential.credential,
        operation="sessions.spawn",
        params={
            "harness": "vibe",
            "prompt": "",
            "cwd": "/tmp",
            "approval": "manual",
            "parent_id": "spoofed-parent",
        },
    )
    assert daemon.registry.get(spawned["id"]).parent_id == actor.id


def test_grants_survive_restart_without_widening_and_cleanup_revokes(theater_home):
    first = Store(paths.db_path())
    try:
        first_registry = Registry(first)
        participant = first_registry.create_spawned(harness="fake", cwd="/tmp")
        credential, credential_path = _attach_credential(
            first,
            participant.id,
            grants=frozenset({PluginCapability.JOBS_READ}),
        )
    finally:
        first.close()

    second = Store(paths.db_path())
    try:
        record = authenticate(SimpleNamespace(store=second), credential.credential)
        assert record.grants == frozenset({PluginCapability.JOBS_READ})
        assert PluginCapability.SESSIONS_SEND not in record.grants
        Registry(second).mark_dead(participant.id)
        assert second.mcp_plugin_credentials(participant.id) == ()
        assert not credential_path.exists()
    finally:
        second.close()


def test_every_declared_capability_has_a_fixed_dispatch_operation():
    assert {operation.capability for operation in OPERATIONS.values()} == set(PluginCapability)


def test_typed_client_reads_the_credential_file_for_each_call_and_maps_denial(tmp_path):
    credential = mint_credential().credential
    credential_file = tmp_path / "credential"
    credential_file.write_text(credential + "\n")

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call(self, method: str, **params):
            self.calls.append((method, params))
            if len(self.calls) == 2:
                raise RemoteError(
                    "capability_denied",
                    "denied",
                    {"required": "jobs.read", "granted": ["participants.read"]},
                )
            return {"id": "p"}

    raw_client = Client()
    plugin_client = TheaterPluginClient(credential_path=credential_file, client=raw_client)
    assert asyncio.run(plugin_client.get_participant("p")) == {"id": "p"}
    credential_file.write_text(credential + "\n")
    with pytest.raises(PluginCapabilityError) as denied:
        asyncio.run(plugin_client.get_participant("p"))
    assert denied.value.required == "jobs.read"
    assert raw_client.calls[0][1]["credential"] == credential
    assert raw_client.calls[1][1]["operation"] == "participants.get"


def test_typed_client_await_uses_the_nested_daemon_await_timeout(tmp_path):
    credential_file = tmp_path / "credential"
    credential_file.write_text(mint_credential().credential + "\n")
    timeouts: list[float] = []

    class Client:
        async def call(self, method: str, **params):
            timeouts.append(DaemonClient._timeout_for(method, params))
            return []

    plugin_client = TheaterPluginClient(credential_path=credential_file, client=Client())
    assert asyncio.run(plugin_client.await_jobs(handles=["p#1"], max_wait=17.0)) == []
    assert timeouts == [17.0 + CALL_TIMEOUT]
    assert (
        DaemonClient._timeout_for(
            "plugin.call", {"operation": "jobs.await", "params": {"max_wait": "bad"}}
        )
        == CALL_TIMEOUT
    )


def test_json_plugin_gateway_has_deterministic_success_and_error_envelopes(monkeypatch, capsys):
    async def call(operation: str, params: dict, credential_file: str | None):
        assert operation == "participants.get"
        assert params == {"id": "p"}
        assert credential_file == "/tmp/credential"
        return {"z": 1, "a": 2}

    args = SimpleNamespace(operation="participants.get", credential_file="/tmp/credential")
    monkeypatch.setattr(plugin_cli, "_call", call)
    monkeypatch.setattr(plugin_cli.sys, "stdin", io.StringIO('{"id":"p"}'))
    assert plugin_cli.cmd_plugin_call(args) == 0
    assert capsys.readouterr().out == '{"ok":true,"result":{"a":2,"z":1}}\n'

    monkeypatch.setattr(plugin_cli.sys, "stdin", io.StringIO("[]"))
    assert plugin_cli.cmd_plugin_call(args) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["ok"] is False
    assert error["error"]["code"] == "bad_request"
