"""Wave-2 contract names in theater.protocol: import surface and key invariants."""

from theater.protocol import (
    BUS_TAIL_MAX_WAIT_SECONDS,
    BUS_TAIL_METHOD,
    BusEvent,
    BusTailParams,
    BusTailResult,
    ProviderReportResult,
    WriteUnit,
)


def test_bus_tail_method_name() -> None:
    assert BUS_TAIL_METHOD == "frontend.bus.tail"
    assert BUS_TAIL_MAX_WAIT_SECONDS == 30.0


def test_bus_tail_params_optional_keys() -> None:
    optional = set(BusTailParams.__optional_keys__)
    assert {"wait_seconds", "kinds"} <= optional


def test_contract_types_usable() -> None:
    event: BusEvent = {
        "id": 1,
        "ts": "2026-10-09T00:00:00Z",
        "kind": "message",
        "from_id": None,
        "to_id": None,
        "payload": {},
    }
    result: BusTailResult = {"items": [event], "next_cursor": None, "next_after_id": 1}
    report: ProviderReportResult = {"presence_invalidated": True}
    assert result and report


def test_write_unit_protocol() -> None:
    class Unit:
        def after_commit(self, hook: object) -> None: ...

    unit: WriteUnit = Unit()  # type: ignore[abstract]
    assert callable(unit.after_commit)
