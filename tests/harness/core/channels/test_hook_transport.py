"""The hook inbox transport: delivery dedupe, health, credentials, bounds and restart."""

from __future__ import annotations

import stat
import time

import pytest

from tests.support.hook_channel_rig import hook_client, hook_event, hook_manifest
from theater.daemon.server import Daemon
from theater.daemon.spawning.planning import (
    install_hook_plan,
    record_launch_identity,
    write_plan_files,
)
from theater.harness.channels import CompositeSource, HookRuntime
from theater.harness.channels.hooks import HookDelivery, HookInbox
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.loading.models import LoadedPlugin
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.registry import _PLUGINS, HARNESSES
from theater.protocol import RemoteError


def test_hook_inbox_transport_dedupes_only_delivery_id() -> None:
    channel = hook_manifest().observation.hook_channels[0].declaration
    inbox = HookInbox()
    assert (
        inbox.enqueue(
            "participant",
            channel,
            HookDelivery(
                event="tool.finished",
                payload={"native_id": "shared"},
                native_id="shared",
                delivery_id="first",
            ),
        ).duplicate
        is False
    )
    assert (
        inbox.enqueue(
            "participant",
            channel,
            HookDelivery(
                event="other.event",
                payload={"native_id": "shared"},
                native_id="shared",
                delivery_id="second",
            ),
        ).duplicate
        is False
    )
    assert (
        inbox.enqueue(
            "participant",
            channel,
            HookDelivery(
                event="tool.finished",
                payload={"native_id": "other"},
                native_id="other",
                delivery_id="first",
            ),
        ).duplicate
        is True
    )
    assert [delivery.event for delivery in inbox.drain("participant", channel.id)] == [
        "tool.finished",
        "other.event",
    ]


def test_hook_runtime_health_snapshot_is_read_only_and_bounded() -> None:
    channel = hook_manifest(max_queue=1).observation.hook_channels[0]
    runtime = HookRuntime(lambda _participant_id, _channel_id: True)

    runtime.enqueue(
        participant_id="participant",
        channel=channel,
        event="tool.finished",
        payload={"native_id": "one", "secret": "do not expose"},
        delivery_id="one",
        native_id="one",
    )
    runtime.enqueue(
        participant_id="participant",
        channel=channel,
        event="tool.finished",
        payload={"native_id": "two"},
        delivery_id="two",
        native_id="two",
    )

    (health,) = runtime.health_snapshot("participant")
    assert health.channel_id == "native-hooks"
    assert health.accepted == 1
    assert health.dropped == 1
    assert health.last_success_at is not None
    assert health.diagnostics == ("hook inbox overflow",)


@pytest.mark.asyncio
async def test_hook_transport_credentials_bounds_decode_and_cleanup(  # noqa: PLR0915
    theater_home, tmp_path, monkeypatch
) -> None:
    manifest = hook_manifest(max_queue=2)
    harness = compile_manifest("acme", manifest)
    daemon = Daemon(harnesses={"acme": harness})
    monkeypatch.setitem(HARNESSES, "acme", harness)
    monkeypatch.setitem(
        _PLUGINS,
        "acme",
        LoadedPlugin(
            path=tmp_path / "acme",
            source="local",
            name="acme",
            harness=harness,
            manifest=manifest,
        ),
    )
    await daemon.start()
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="p-hook")
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    write_plan_files(plan)
    credential = plan.channel_credentials[0]
    assert stat.S_IMODE(credential.token_path.stat().st_mode) == 0o600
    assert credential.token not in (credential.token_path.with_name("native-hook.json")).read_text()
    assert plan.env["ACME_HOOK_CONFIG"].endswith("native-hook.json")

    client = await hook_client(daemon)
    try:
        first = await hook_event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="one",
            delivery_id="delivery-one",
        )
        duplicate = await hook_event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="one",
            delivery_id="delivery-one",
        )
        native_duplicate = await hook_event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="one",
            delivery_id="delivery-one-retry",
        )
        overflow = await hook_event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="two",
            delivery_id="delivery-two",
        )
        assert first == {"ok": True, "duplicate": False, "dropped": False}
        assert duplicate == {"ok": True, "duplicate": True, "dropped": False}
        assert native_duplicate == {"ok": True, "duplicate": False, "dropped": False}
        assert overflow == {"ok": True, "duplicate": False, "dropped": True}
        rows = await client.call("harnesses")
        row = next(row for row in rows if row["name"] == "acme")
        runtime = row["runtime"]
        assert runtime["state"] == "active"
        participant_runtime = runtime["participants"][0]
        assert participant_runtime["participant_id"] == participant.id
        assert participant_runtime["channels"] == [
            {
                "id": "native-hooks",
                "state": "degraded",
                "diagnostics": ["hook inbox overflow"],
                "dropped": 1,
                "accepted": 2,
                "last_success_at": pytest.approx(time.time(), abs=2),
            }
        ]
        assert credential.token not in str(runtime)
        assert "native_id" not in str(runtime)
        assert await hook_event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="two",
            delivery_id="delivery-two",
        ) == {"ok": True, "duplicate": True, "dropped": False}

        source = daemon.observer._open_source(participant.id, harness.observer)
        assert isinstance(source, CompositeSource)
        batch = await source.read()
        assert [fact.native_id for fact in batch.trajectory] == ["one"]
        hook_source = source._enrichments[0].source
        health = hook_source.channel_health()
        assert health is not None and health.dropped == 1
        assert source.channel_health()[0].dropped == 1

        await hook_event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="boom",
            delivery_id="delivery-boom",
        )
        await source.read()
        assert hook_source.channel_health().state.value == "degraded"
        await hook_event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="wrong",
            delivery_id="delivery-wrong",
            payload={"mode": "wrong"},
        )
        await source.read()
        assert hook_source.channel_health().state.value == "degraded"

        with pytest.raises(RemoteError, match="credential is invalid"):
            await hook_event(client, pid=participant.id, token="forged", native_id="x")
        with pytest.raises(RemoteError, match="non-empty string"):
            await hook_event(client, pid=participant.id, token="", native_id="x")
        with pytest.raises(RemoteError, match="credential is invalid"):
            await hook_event(client, pid=participant.id, token="x" * 129, native_id="x")
        other = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="p-other")
        with pytest.raises(RemoteError, match="credential is invalid"):
            await hook_event(client, pid=other.id, token=credential.token, native_id="x")
        with pytest.raises(RemoteError, match="credential is invalid"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="other-channel",
                event="tool.finished",
                payload={"native_id": "x"},
            )
        with pytest.raises(RemoteError, match="harness does not match"):
            await hook_event(
                client,
                pid=participant.id,
                token=credential.token,
                native_id="x",
                harness="other",
            )
        with pytest.raises(RemoteError, match="payload exceeds"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="native-hooks",
                event="tool.finished",
                payload={"native_id": "x", "large": "x" * 512},
            )
        with pytest.raises(RemoteError, match="JSON object"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="native-hooks",
                event="tool.finished",
                payload=[],
            )
        with pytest.raises(RemoteError, match="too many attributes"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="native-hooks",
                event="tool.finished",
                payload={f"key-{i}": i for i in range(200)},
            )
        nested = {"native_id": "x"}
        for _ in range(10):
            nested = {"next": nested}
        with pytest.raises(RemoteError, match="nesting"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="native-hooks",
                event="tool.finished",
                payload=nested,
            )
        with pytest.raises(RemoteError, match="not declared"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="native-hooks",
                event="unknown",
                payload={"native_id": "x"},
            )

        daemon.registry.mark_dead(participant.id)
        assert not credential.token_path.exists()
        assert hook_source.channel_health() is None
        await source.aclose()
    finally:
        await client.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_hook_credential_survives_runtime_restart(theater_home, tmp_path) -> None:
    harness = compile_manifest("acme", hook_manifest())
    daemon = Daemon(harnesses={"acme": harness})
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="p-restart")
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    write_plan_files(plan)
    credential = plan.channel_credentials[0]
    await daemon.aclose()

    restarted = Daemon(harnesses={"acme": harness})
    await restarted.start()
    client = await hook_client(restarted)
    try:
        result = await hook_event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="after-restart",
        )
        assert result["ok"] is True
        source = restarted.observer._open_source(participant.id, harness.observer)
        assert source is not None
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["after-restart"]
        await source.aclose()
    finally:
        await client.aclose()
        await restarted.aclose()
