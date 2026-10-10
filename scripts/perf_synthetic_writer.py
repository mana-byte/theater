#!/usr/bin/env python3
"""Measurement-only transcript writer: appends JSONL records stamped at write time.

Stands in for an agent CLI under the `synth` plugin (scripts/perf_synth_plugin) so
`transcript_to_bus` becomes write-to-bus: theater-side latency at a deterministic event rate.
Stdlib only; runs unchanged against any commit. Rate 0 creates the file and idles.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WriterStats:
    written: int = 0
    skipped: int = 0  # slots missed because the writer fell behind (never burst-caught-up)
    elapsed: float = 0.0

    @property
    def achieved_rate(self) -> float:
        # n records span n-1 intervals; `elapsed` runs from the first (slot 0) to the last write.
        return (self.written - 1) / self.elapsed if self.written > 1 and self.elapsed else 0.0


def record_line(seq: int, ts: float) -> bytes:
    """One record; `ts` is the harness-record timestamp the perf script reads."""
    body = {"type": "assistant", "seq": seq, "ts": ts, "text": f"synthetic record {seq}"}
    return (json.dumps(body, separators=(",", ":")) + "\n").encode()


def write_records(
    path: Path,
    rate: float,
    *,
    stop: threading.Event,
    count: int | None = None,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> WriterStats:
    """Append `rate` records/s on an absolute schedule until `stop` or `count` records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = WriterStats()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        if rate <= 0:
            stop.wait()
            return stats
        period = 1.0 / rate
        start = monotonic()
        slot = 0
        while not stop.is_set() and (count is None or stats.written < count):
            wait = start + slot * period - monotonic()
            if wait > 0 and stop.wait(wait):
                break
            ts = clock()  # stamped immediately before the write
            os.write(fd, record_line(stats.written, ts))
            stats.written += 1
            behind = int((monotonic() - start) / period)
            if behind > slot:  # fell behind: skip missed slots instead of bursting
                stats.skipped += behind - slot
                slot = behind
            slot += 1
            stats.elapsed = monotonic() - start
    finally:
        os.close(fd)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--rate", type=float, default=0.0, help="records per second; 0 = idle")
    parser.add_argument("--count", type=int, default=None)
    args = parser.parse_args(argv)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    stats = write_records(args.file, args.rate, stop=stop, count=args.count)
    print(json.dumps({**stats.__dict__, "achieved_rate": stats.achieved_rate}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
