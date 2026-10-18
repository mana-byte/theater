"""Write a local harness plugin package and install it through the real loader."""

from __future__ import annotations

from pathlib import Path

from theater import config as cfg
from theater import harness as harness_registry
from theater.harness.loading.discovery import MANIFEST_FILENAME

MANIFEST_BODY = """
from theater.harness.contracts.callbacks import LaunchContext, ScreenContext
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    HarnessManifest,
    LaunchManifest,
    ObservationManifest,
    ScreenManifest,
)
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind, ScreenReading

def _plan(context):
    return LaunchPlan(
        argv=["{binary}", context.participant_id],
        env={{"ID": context.participant_id}},
    )

def _screen(_context):
    return ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH)

MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION,
    binary="{binary}",
    icon={icon},
    aliases={aliases},
    launch=LaunchManifest(planner=_plan, approvals=frozenset({{"manual"}})),
    observation=ObservationManifest(
        primary=None,
        screen=ScreenManifest(classifier=_screen),
    ),
)
"""


def write_plugin(
    dirpath: Path,
    name: str = "acme",
    *,
    binary: str = "acme",
    icon: str = "@",
    aliases: str = "()",
) -> Path:
    """Create a named package manifest under ``dirpath``."""
    pkg = dirpath / name
    pkg.mkdir(parents=True, exist_ok=True)
    manifest_path = pkg / MANIFEST_FILENAME
    manifest_path.write_text(
        MANIFEST_BODY.format(binary=binary, icon=repr(icon), aliases=aliases),
        encoding="utf-8",
    )
    return pkg


def install(local: Path, config: cfg.Config | None = None, **kwargs) -> list[str]:
    return harness_registry.install(config or cfg.Config(), local_dir=local, **kwargs)
