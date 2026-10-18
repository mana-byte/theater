"""Manifests, installers and clients for the hook-channel tests."""

from __future__ import annotations

from tests.support.channel_fixtures import launch_plan, primary_source, prompt_screen
from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.harness.contracts.callbacks import (
    HookCorrelationContext,
    HookDecodeContext,
    HookInstallContext,
    HookInstallOverlay,
)
from theater.harness.contracts.channels import (
    ChannelBounds,
    ChannelCapability,
    ChannelDeclaration,
    ChannelFact,
    ChannelKind,
    HookBinding,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    HarnessManifest,
    HookChannelManifest,
    LaunchManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory.enums import TrajectoryKind


def hook_correlation(context: HookCorrelationContext) -> str:
    native_id = context.payload.get("native_id")
    if not isinstance(native_id, str):
        raise TypeError("missing native_id")
    return native_id


def hook_decode(context: HookDecodeContext) -> tuple[ChannelFact, ...]:
    native_id = context.native_id
    if native_id == "boom":
        raise RuntimeError("decoder failed")
    if context.payload.get("mode") == "wrong":
        native_id = "other"
    revision = context.payload.get("revision", 0)
    if type(revision) is not int:
        raise TypeError("revision must be an integer")
    return (
        ChannelFact(
            SignalKind.TOOL,
            TrajectoryFact(kind=TrajectoryKind.TOOL_CALL, native_id=native_id, revision=revision),
        ),
    )


def install(context: HookInstallContext) -> HookInstallOverlay:
    config = context.token_file.with_name("native-hook.json")
    return HookInstallOverlay(
        env={"ACME_HOOK_CONFIG": str(config)},
        files={config: f"{context.participant_id}:{context.channel_id}:{context.token_file}"},
    )


def hook_manifest(
    *,
    max_queue: int = 2,
    primary: bool = True,
    correlation=hook_correlation,
    decoder=hook_decode,
) -> HarnessManifest:
    channel = HookChannelManifest(
        declaration=ChannelDeclaration(
            id="native-hooks",
            kind=ChannelKind.HOOK,
            capabilities=(ChannelCapability(SignalKind.TOOL, SignalOwnership.ENRICHMENT),),
            bounds=ChannelBounds(max_queue=max_queue, max_payload_bytes=256),
        ),
        bindings=(
            HookBinding(
                event="tool.finished",
                signals=(SignalKind.TOOL,),
                decoder=decoder,
                correlation=correlation,
            ),
        ),
        installer=install,
    )
    return HarnessManifest(
        api_version=MANIFEST_API_VERSION,
        binary="acme",
        icon="A",
        launch=LaunchManifest(planner=launch_plan, approvals=("manual",)),
        observation=ObservationManifest(
            primary=(
                SourceManifest(
                    factory=primary_source,
                    channel=ChannelDeclaration(id="primary", kind=ChannelKind.TRANSCRIPT),
                )
                if primary
                else None
            ),
            screen=ScreenManifest(classifier=prompt_screen),
            enrichments=(channel,),
        ),
    )


async def hook_client(daemon: Daemon) -> DaemonClient:
    client = DaemonClient(autostart=False)
    await client.connect()
    return client


async def hook_event(client: DaemonClient, *, pid: str, token: str, native_id: str, **extra):
    payload = {"native_id": native_id, **extra.pop("payload", {})}
    return await client.call(
        "harness.event",
        id=pid,
        token=token,
        channel="native-hooks",
        event="tool.finished",
        payload=payload,
        **extra,
    )
