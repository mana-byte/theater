"""Bounded-lifetime display probes; launch admission never consumes this cache."""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from theater.daemon import workers
from theater.harness.contracts.runtime import (
    RuntimeCompatibility,
    RuntimeCompatibilityProbe,
    RuntimeProbeContext,
)

PROBE_CACHE_SECONDS = 60.0


def _fingerprint(binary: str | None) -> tuple[object, ...] | None:
    resolved = shutil.which(binary) if binary else None
    if resolved is None:
        return None
    try:
        path = Path(resolved).resolve(strict=True)
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@dataclass(frozen=True, slots=True)
class _CachedProbe:
    callback: RuntimeCompatibilityProbe
    context: RuntimeProbeContext
    configuration: object
    fingerprint: tuple[object, ...]
    expires_at: float
    result: RuntimeCompatibility


class CompatibilityProbeCache:
    """Coalesce catalog probes and invalidate on binary, policy, or config changes."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, _CachedProbe] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def probe(
        self,
        name: str,
        callback: RuntimeCompatibilityProbe,
        context: RuntimeProbeContext,
        *,
        configuration: object,
    ) -> RuntimeCompatibility | None:
        async with self._locks.setdefault(name, asyncio.Lock()):
            fingerprint = await workers.to_thread(
                _fingerprint, context.binary, label="harnesses.binary_identity"
            )
            cached = self._entries.get(name)
            if (
                cached is not None
                and cached.callback is callback
                and cached.context == context
                and cached.configuration is configuration
                and cached.fingerprint == fingerprint
                and self._clock() < cached.expires_at
            ):
                return cached.result
            self._entries.pop(name, None)
            result = await workers.to_thread(
                callback, context, label="harnesses.native_compatibility"
            )
            if not isinstance(result, RuntimeCompatibility):
                return None
            current = await workers.to_thread(
                _fingerprint, context.binary, label="harnesses.binary_identity"
            )
            if fingerprint is not None and current == fingerprint:
                self._entries[name] = _CachedProbe(
                    callback,
                    context,
                    configuration,
                    fingerprint,
                    self._clock() + PROBE_CACHE_SECONDS,
                    result,
                )
            elif current != fingerprint:
                return None
            return result
