"""Catalog probe reuse never outlives the executable or configuration identity."""

from __future__ import annotations

import asyncio

import pytest

from theater.daemon.harness_runtime.compatibility import CompatibilityProbeCache
from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext


@pytest.mark.parametrize("change", ["binary", "configuration", "callback", "expiry"])
async def test_catalog_probes_coalesce_and_invalidate(tmp_path, change):
    binary = tmp_path / "agent"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    clock = [0.0]
    cache = CompatibilityProbeCache(clock=lambda: clock[0])
    configuration = object()
    context = RuntimeProbeContext(binary=str(binary))
    calls = []

    def callback(context):
        calls.append(context)
        return RuntimeCompatibility(supported=True, native_version=str(len(calls)))

    async def probe():
        return await cache.probe("agent", callback, context, configuration=configuration)

    results = await asyncio.gather(*(probe() for _ in range(4)))
    assert len(calls) == 1
    assert all(result.native_version == "1" for result in results)

    if change == "binary":
        binary.write_text("#!/bin/sh\nexit 42\n")
    elif change == "configuration":
        configuration = object()
    elif change == "callback":
        original = callback

        def callback(context):
            return original(context)
    else:
        clock[0] = 61
    assert (await probe()).native_version == "2"
    assert (await probe()).native_version == "2"
    assert len(calls) == 2


async def test_changed_binary_during_probe_does_not_publish_stale_compatibility(tmp_path):
    binary = tmp_path / "agent"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    cache = CompatibilityProbeCache()

    def callback(context):
        binary.write_text("#!/bin/sh\nexit 42\n")
        return RuntimeCompatibility(supported=True)

    assert (
        await cache.probe(
            "agent", callback, RuntimeProbeContext(binary=str(binary)), configuration=None
        )
        is None
    )
