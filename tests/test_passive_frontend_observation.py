"""Optional frontend observations cannot replace durable completion or identity."""

import pytest

from tests.test_hybrid_source import ScriptedSource, outcome
from tests.test_pi_native_bridge import (
    GENERATION,
    PARTICIPANT,
    SESSION_A,
    SESSION_B,
    PiFrontendRuntime,
    ScriptedPiPeer,
    bridge_snapshot,
)
from theater.harness.builtin.plugins.opencode.live import OpenCodeTuiLiveSource
from theater.harness.channels.composite import CompositeSource, EnrichmentBinding
from theater.harness.channels.hybrid import HybridSource
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.runtime import LiveChannelDeclaration, RuntimeNotification
from theater.harness.contracts.source import Batch, Source
from theater.models import Status

PASSIVE = LiveChannelDeclaration(
    channel=ChannelDeclaration(id="pi-frontend-live", kind=ChannelKind.LIVE),
    drives_job_completion=False,
)


async def test_passive_frontend_never_completes_a_legacy_delivered_turn():
    durable = ScriptedSource(Batch(status=Status.WORKING), Batch(status=Status.WORKING))
    live = ScriptedSource(Batch(status=Status.IDLE, terminal_evidence=(outcome(),)), Batch())
    source = HybridSource(durable=durable, live=live, live_channel=PASSIVE)
    first = await source.read()
    assert first.status is Status.IDLE
    assert first.terminal_evidence == ()
    # An old idle observation is not retained after the live source stops
    # confirming it; the ordinary transcript remains the fallback.
    assert (await source.read()).status is Status.WORKING


async def test_passive_pi_identity_is_rechecked_after_sibling_enrichment_awaits():
    trusted = [SESSION_A]
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = PiFrontendRuntime(
        participant_id=PARTICIPANT,
        backend_generation=GENERATION,
        peer=peer,
        trusted_session_id_provider=lambda: trusted[0],
    )
    await runtime.attach()

    class SessionSwitch(Source):
        async def read(self):
            trusted[0] = SESSION_B
            return Batch()

    source = CompositeSource(
        primary=HybridSource(
            durable=ScriptedSource(Batch(status=Status.WORKING)),
            live=runtime.live_source(),
            live_channel=PASSIVE,
        ),
        enrichments=(
            EnrichmentBinding(
                SessionSwitch(),
                ChannelDeclaration(id="late-hook", kind=ChannelKind.HOOK),
            ),
        ),
    )
    try:
        assert (await source.read()).status is Status.WORKING
        assert (await runtime.live_source().read()).status is None
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("change", ["trusted", "visible", "home", "disconnect", "expire", "busy"])
async def test_opencode_status_is_revalidated_after_sibling_await(change, monkeypatch):
    trusted = ["ses-a"]
    clock = [100.0]
    monkeypatch.setattr("theater.harness.builtin.plugins.opencode.live.monotonic", lambda: clock[0])
    live = OpenCodeTuiLiveSource(lambda: trusted[0])

    def publish(session, epoch, status):
        live.feed(
            RuntimeNotification(
                method="snapshot",
                params={
                    "session_id": session,
                    "route_session_id": session,
                    "session_epoch": epoch,
                    "status": {"type": status},
                },
            )
        )

    publish("ses-a", 1, "idle")

    class LaterSource(Source):
        async def read(self):
            if change == "trusted":
                trusted[0] = "ses-b"
            elif change == "visible":
                publish("ses-b", 2, "idle")
            elif change == "home":
                publish(None, 2, "idle")
            elif change == "disconnect":
                live.disconnected()
            elif change == "expire":
                clock[0] += 4
            else:
                publish("ses-a", 1, "busy")
            return Batch()

    source = CompositeSource(
        primary=HybridSource(
            durable=ScriptedSource(
                Batch(status=Status.IDLE if change == "busy" else Status.WORKING)
            ),
            live=live,
            live_channel=LiveChannelDeclaration(
                channel=ChannelDeclaration(id="opencode-tui-live", kind=ChannelKind.LIVE),
                drives_job_completion=False,
            ),
        ),
        enrichments=(
            EnrichmentBinding(LaterSource(), ChannelDeclaration(id="late", kind=ChannelKind.HOOK)),
        ),
    )
    batch = await source.read()
    assert batch.status is Status.WORKING
    assert batch.terminal_evidence == ()
