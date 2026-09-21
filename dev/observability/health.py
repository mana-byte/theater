"""Check historical trace readability without creating traffic or changing the stack."""

from __future__ import annotations

import argparse
import json
import time
from urllib.parse import urlencode
from urllib.request import urlopen

_TEMPO = "http://127.0.0.1:3000/api/datasources/proxy/uid/tempo"
_RESPONSE_BYTES = 8 * 1024 * 1024


def query(path: str, **params: object) -> dict:
    url = f"{_TEMPO}/{path}?{urlencode(params)}"
    with urlopen(url, timeout=10) as response:
        raw = response.read(_RESPONSE_BYTES + 1)
    if len(raw) > _RESPONSE_BYTES:
        raise ValueError("Tempo health response exceeds its bounded read limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("Tempo health response must be an object")
    return value


def inspect_history(*, now: int, hours: int = 8) -> str:
    """Require a stored trace older than the live-store query and retention windows."""
    response = query("api/search", q="{}", start=now - hours * 3600, end=now - 30 * 60, limit=1)
    traces = response.get("traces", [])
    if not traces:
        raise ValueError(
            "Historical trace health is unproven: no trace was found outside live storage. "
            "On an established stack, inspect Tempo tenant-index errors and block metadata; "
            "on a new or idle stack, retry after telemetry has aged at least 30 minutes."
        )
    trace_id = traces[0].get("traceID")
    if (
        not isinstance(trace_id, str)
        or not 1 <= len(trace_id) <= 32
        or any(char not in "0123456789abcdef" for char in trace_id)
        or not int(trace_id, 16)
    ):
        raise ValueError("Tempo returned an invalid historical trace identity")
    # Tempo's search response omits leading zeroes in trace IDs.
    trace_id = trace_id.zfill(32)
    trace = query(f"api/traces/{trace_id}")
    if not any(
        scope.get("spans")
        for batch in trace.get("batches", [])
        for scope in batch.get("scopeSpans", [])
    ):
        raise ValueError(
            "Historical search succeeded, but the selected trace has no readable spans"
        )
    return trace_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, choices=range(1, 25), default=8)
    args = parser.parse_args()
    try:
        trace_id = inspect_history(now=int(time.time()), hours=args.hours)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise SystemExit(f"Historical tracing check failed: {exc}") from exc
    print(f"Verified historical trace search and retrieval (>30 minutes old): {trace_id}")


if __name__ == "__main__":
    main()
