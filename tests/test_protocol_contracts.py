"""Wave-2 contract names in theater.protocol: import surface and key invariants."""

from theater.protocol import (
    BUS_TAIL_MAX_WAIT_SECONDS,
    BUS_TAIL_METHOD,
    BusTailParams,
    ProviderReportResult,
)


def test_wave2_contracts() -> None:
    assert BUS_TAIL_METHOD == "frontend.bus.tail"
    assert BUS_TAIL_MAX_WAIT_SECONDS == 30.0
    assert {"wait_seconds", "kinds"} <= set(BusTailParams.__optional_keys__)
    assert "invalidated_terminals" in ProviderReportResult.__optional_keys__
