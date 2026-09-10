"""Bounded channel composition for harness sources."""

from __future__ import annotations

from theater.harness.channels.composite import CompositeSource, EnrichmentBinding
from theater.harness.channels.health import ChannelHealthTracker
from theater.harness.channels.hooks import HookRuntime, HookSource
from theater.harness.channels.hybrid import HybridSource, HybridSourceError
from theater.harness.channels.otel import NativeOtelRuntime, OtelSource
from theater.harness.channels.wakeup import WakeupHub, WakeupSignal

__all__ = [
    "ChannelHealthTracker",
    "CompositeSource",
    "EnrichmentBinding",
    "HookRuntime",
    "HookSource",
    "HybridSource",
    "HybridSourceError",
    "NativeOtelRuntime",
    "OtelSource",
    "WakeupHub",
    "WakeupSignal",
]
