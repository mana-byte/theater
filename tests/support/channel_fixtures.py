"""The primary source, screen reader and launch plan of the fake harness channel tests build."""

from __future__ import annotations

from theater.harness.contracts.callbacks import (
    LaunchContext,
    ScreenContext,
)
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.harness.contracts.source import Batch, Source


class PrimarySource(Source):
    async def read(self) -> Batch:
        return Batch()


def primary_source(_context) -> Source:
    return PrimarySource()


def prompt_screen(_context: ScreenContext) -> ScreenReading:
    return ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH)


def launch_plan(context: LaunchContext) -> LaunchPlan:
    return LaunchPlan(argv=["acme", context.participant_id])
