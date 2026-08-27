"""Bounded channel composition for harness sources."""

from __future__ import annotations

from theater.harness.channels.composite import CompositeSource, EnrichmentBinding
from theater.harness.channels.health import ChannelHealthTracker
from theater.harness.channels.hooks import HookRuntime, HookSource
from theater.harness.channels.otel import NativeOtelRuntime, OtelSource

__all__ = [
    "ChannelHealthTracker",
    "CompositeSource",
    "EnrichmentBinding",
    "HookRuntime",
    "HookSource",
    "NativeOtelRuntime",
    "OtelSource",
]
