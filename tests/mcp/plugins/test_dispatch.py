"""Plugin dispatch: per-call authentication, credential-owned identity, grants, typed client."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.paths import fixture
from tests.support.plugin_sidecars import init_git_repo
from theater import paths
from theater.cli.commands import plugin as plugin_cli
from theater.client import CALL_TIMEOUT, DaemonClient
from theater.daemon.persistence.store import Store
from theater.daemon.plugins.credentials import mint_credential
from theater.daemon.plugins.dispatch import OPERATIONS, authenticate
from theater.daemon.registry import Registry
from theater.daemon.spawning import planning
from theater.harness.base import LaunchPlan
from theater.mcp_plugins import (
    MCP_PLUGIN_API_VERSION,
    PluginCapability,
)
from theater.models import Status
from theater.plugin_client import PluginCapabilityError, TheaterPluginClient
from theater.protocol import RemoteError


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
    credential_path = paths.participant_plugin_dir(participant_id, name) / "credential"
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
    daemon, client, terminal_provider
):
    actor = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    credential, _path = _attach_credential(
        daemon.store,
        actor.id,
        grants=frozenset({PluginCapability.SESSIONS_SEND}),
    )
    target = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")
    trusted = daemon.registry.get(target["id"])
    trusted.session_id = "trusted-target"
    trusted.session_correlation = "operator"
    daemon.store.upsert_participant(trusted)
    terminal_id = terminal_provider.bind(daemon, target["id"])
    terminal_provider.screens[terminal_id] = (fixture("screens", "vibe_idle.txt")).read_text()
    daemon.registry.set_status(target["id"], Status.WORKING)

    with pytest.raises(RemoteError) as busy:
        await client.call(
            "plugin.call",
            credential=credential.credential,
            operation="sessions.send",
            params={
                "target": target["id"],
                "prompt": "work",
                "caller_id": "spoofed",
                "if_busy": "refuse",
            },
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
    assert terminal_provider.deliveries[-1] == (terminal_id, "work")


async def test_plugin_scratchpad_delete_forwards_every_selector(client, daemon, tmp_path):
    repo = tmp_path / "repo"
    init_git_repo(repo)
    actor = daemon.registry.create_spawned(harness="fake", cwd=str(repo))
    _credential, credential_path = _attach_credential(
        daemon.store, actor.id, grants=frozenset({PluginCapability.SCRATCHPAD_DELETE})
    )
    typed = TheaterPluginClient(credential_path=credential_path, client=client)
    digest = hashlib.sha256(b"gone").hexdigest()

    # each selector travels alone: a dropped parameter refuses the call
    deleted = await typed.scratchpad_delete(namespace="notes", keys=["gone"])
    assert deleted == {"namespace": "notes", "deleted": []}
    deleted = await typed.scratchpad_delete(namespace="notes", digests=[digest])
    assert deleted == {"namespace": "notes", "deleted": []}
    cleared = await typed.scratchpad_delete(namespace="notes", clear=True)
    assert cleared == {"namespace": "notes", "deleted_count": 0}


async def test_plugin_preserves_parent_filters_but_forces_spawn_parent_identity(
    daemon, client, terminal_provider
):
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

    typed_spawn = await typed.spawn_session(
        harness="vibe",
        approval="manual",
        cwd="/tmp",
        background=False,
        window_name="custom-window",
        tmux_session="custom-session",
    )
    assert daemon.registry.get(typed_spawn["id"]).parent_id == actor.id
    presentation = terminal_provider.creations[-1]
    assert presentation["background"] is True
    assert presentation["name"] == f"vibe-{typed_spawn['id'][:6]}"


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
