"""Receipt values assembled from durable provider launch facts."""

from __future__ import annotations

from regie.bridge.state import DurableLaunchIntent


def accepted_create_receipt(
    intent: DurableLaunchIntent, terminal: dict[str, object]
) -> dict[str, object]:
    return {
        "operation_id": intent.operation_id,
        "provider_generation": intent.provider_generation,
        "outcome": "accepted",
        "terminal": terminal,
        "launch_id": intent.launch_id,
    }
