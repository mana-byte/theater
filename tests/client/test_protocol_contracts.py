"""Wave-2 contract names in theater.protocol: import surface and key invariants."""

from typing import get_type_hints

from theater.daemon.persistence.transactions import WriteUnit as DaemonWriteUnit
from theater.protocol import (
    BUS_TAIL_MAX_WAIT_SECONDS,
    BUS_TAIL_METHOD,
    BusEvent,
    BusTailParams,
    ProviderReportFacts,
    ProviderReportResult,
    WriteUnit,
)


def test_wave2_contracts() -> None:
    assert BUS_TAIL_METHOD == "frontend.bus.tail"
    assert BUS_TAIL_MAX_WAIT_SECONDS == 30.0
    assert {"wait_seconds", "kinds"} <= set(BusTailParams.__optional_keys__)
    assert "invalidated_terminals" in ProviderReportFacts.__optional_keys__
    assert "presence_invalidated" not in ProviderReportResult.__annotations__
    assert get_type_hints(BusEvent)["ts"] is float


def test_write_unit_mirrors_daemon_interface() -> None:
    def public(cls: type) -> set[str]:
        return {
            name
            for name in vars(cls)
            if name in {"__enter__", "__exit__"} or not name.startswith("_")
        }

    assert public(WriteUnit) == public(DaemonWriteUnit)
