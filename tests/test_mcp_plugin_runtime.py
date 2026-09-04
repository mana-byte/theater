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
    mcp_registry.MCP_SERVERS.clear()
    try:
        yield mcp_registry.MCP_SERVERS
    finally:
        mcp_registry.MCP_SERVERS.clear()
        mcp_registry.MCP_SERVERS.update(prior)


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
    monkeypatch, registry, isolated_mcp_registry
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
    (spec,) = observed[0]
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
    monkeypatch, registry, isolated_mcp_registry, tmp_path
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
    assert [item.name for item in seen[0]] == ["good"]
    attached = registry.store.mcp_plugin_credentials(participant.id)
    assert [item.plugin_name for item in attached] == ["good"]
    events = registry.store.bus_tail(limit=20)
    assert any(
        event["kind"] == "mcp_plugin.omitted" and event["payload"]["plugin"] == "bad"
        for event in events
    )


def test_sidecar_artifact_collisions_are_omitted_without_overwriting_harness_files(
    monkeypatch, registry, isolated_mcp_registry
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

    assert [len(servers) for servers in observed] == [1, 0]
    assert plan.files == {root / "config" / "harness.json": "{}"}
    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert root / ".theater-plugin-credential" not in plan.private_files


def test_sidecar_persistence_failure_rolls_back_its_record_and_empty_root(
    monkeypatch, registry, isolated_mcp_registry
):
    isolated_mcp_registry["acme"] = _plugin()
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    root = paths.participant_mcp_plugin_dir(participant.id, "acme")
    persist = registry.store.set_mcp_plugin_credential

    def persist_then_fail(*args, **kwargs) -> None:
        persist(*args, **kwargs)
        raise RuntimeError("simulated persistence failure")

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        assert mcp_servers == ()
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(registry.store, "set_mcp_plugin_credential", persist_then_fail)
    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    assert planning.build_plan(_request(), participant, None, registry=registry).argv == ["fake"]

    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert not root.exists()


def test_harness_rejection_of_sidecars_omits_them_without_failing_the_spawn_plan(
    monkeypatch, registry, isolated_mcp_registry
):
    isolated_mcp_registry["acme"] = _plugin()
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    attempts: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        attempts.append(mcp_servers)
        if mcp_servers:
            raise ValueError("renderer cannot use this sidecar")
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    assert planning.build_plan(_request(), participant, None, registry=registry).argv == ["fake"]

    assert [len(servers) for servers in attempts] == [1, 0]
    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert any(
        event["kind"] == "mcp_plugin.omitted" and event["payload"]["plugin"] == "acme"
        for event in registry.store.bus_tail(limit=20)
    )


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
