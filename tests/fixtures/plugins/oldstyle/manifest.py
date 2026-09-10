"""An old-style local harness plugin: no ``runtime`` field anywhere.

This fixture is the compatibility proof for the runtime-contracts wave: a
plugin authored before native wiring existed must keep launching, observing,
sending, and interrupting exactly as before. ``HarnessManifest.runtime``
defaults to ``None`` and nothing below knows the runtime contracts exist.

Install it by copying this directory to ``$THEATER_HOME/plugins/oldstyle/``
and scanning; see ``tests/test_harness_runtime_contracts.py``.
"""

from __future__ import annotations

from theater.harness.contracts.callbacks import LaunchContext, ScreenContext
from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    HarnessManifest,
    LaunchManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.contracts.observation import ScreenKind, ScreenReading
from theater.harness.contracts.source import Batch, Source


def plan_launch(context: LaunchContext) -> LaunchPlan:
    argv = [context.config_path.name, "--mcp-config", str(context.config_path)]
    if context.prompt:
        argv.append(context.prompt)
    return LaunchPlan(
        argv=["acme", *argv],
        env={"ACME_HOME": "/tmp"},
        files={
            context.config_path: "{}",
        },
    )


def classify_screen(context: ScreenContext) -> ScreenReading:
    last = context.capture.rstrip().splitlines()[-1] if context.capture else ""
    if last.endswith("❯"):
        return ScreenReading(kind=ScreenKind.PROMPT, confidence=0.9)
    return ScreenReading(kind=ScreenKind.BUSY, confidence=0.5)


class OldStyleSource(Source):
    """A source that never attaches and never reports native evidence."""

    async def read(self) -> Batch:
        return Batch(waiting=True)


def source_factory(context) -> Source:
    del context
    return OldStyleSource()


MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION,
    binary="acme",
    icon="A",
    launch=LaunchManifest(
        planner=plan_launch,
        approvals=("manual", "edits", "yolo"),
        supports_model=False,
        supports_reasoning_effort=False,
        supports_resume=False,
    ),
    observation=ObservationManifest(
        primary=SourceManifest(
            factory=source_factory,
            channel=ChannelDeclaration(
                id="transcript",
                kind=ChannelKind.TRANSCRIPT,
                capabilities=(
                    ChannelCapability(SignalKind.IDENTITY, SignalOwnership.PRIMARY),
                    ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),
                    ChannelCapability(SignalKind.TURN, SignalOwnership.PRIMARY),
                ),
            ),
        ),
        screen=ScreenManifest(classifier=classify_screen),
    ),
)
