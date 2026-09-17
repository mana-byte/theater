"""Forward-compatible public catalog entries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from theater.frontend.dto._wire import (
    JSONValue,
    append_extras,
    boolean_value,
    extras,
    freeze_json,
    object_value,
    string_value,
    thaw_json,
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    entry_id: str
    available: bool
    reason: str | None = None
    details: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> CatalogEntry:
        data = object_value(value, "catalog entry")
        entry_id = data.get("entry_id")
        available = data.get("available")
        reason = data.get("reason")
        if not isinstance(entry_id, str) or not entry_id:
            raise TypeError("catalog entry.entry_id must be a non-empty string")
        if type(available) is not bool:
            raise TypeError("catalog entry.available must be a boolean")
        if reason is not None and not isinstance(reason, str):
            raise TypeError("catalog entry.reason must be a string or null")
        details = freeze_json(object_value(data.get("details", {}), "catalog entry.details"))
        assert isinstance(details, Mapping)
        return cls(
            entry_id=entry_id,
            available=available,
            reason=reason,
            details=details,
            extra=extras(data, {"entry_id", "available", "reason", "details"}),
        )

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "entry_id": self.entry_id,
                "available": self.available,
                "reason": self.reason,
                "details": thaw_json(self.details),
            },
            self.extra,
        )


@dataclass(frozen=True, slots=True)
class HarnessCatalogEntry:
    name: str
    installed: bool
    compatible: bool
    supported_wiring: tuple[str, ...]
    requires_terminal: bool
    provider_ready: bool
    launch_available: bool
    reason: str | None = None
    detail: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> HarnessCatalogEntry:
        data = object_value(value, "harness catalog entry")
        raw_wiring = data.get("supported_wiring")
        if not isinstance(raw_wiring, (list, tuple)):
            raise TypeError("harness catalog entry.supported_wiring must be an array")
        booleans = (
            "installed",
            "compatible",
            "requires_terminal",
            "provider_ready",
            "launch_available",
        )
        if any(type(data.get(key)) is not bool for key in booleans):
            raise TypeError("harness catalog readiness fields must be booleans")
        reason = data.get("reason")
        detail = data.get("detail")
        if reason is not None and not isinstance(reason, str):
            raise TypeError("harness catalog entry.reason must be a string or null")
        if detail is not None and not isinstance(detail, str):
            raise TypeError("harness catalog entry.detail must be a string or null")
        return cls(
            name=string_value(data.get("name"), "harness catalog entry.name") or "",
            installed=boolean_value(data["installed"], "harness catalog entry.installed"),
            compatible=boolean_value(data["compatible"], "harness catalog entry.compatible"),
            supported_wiring=tuple(
                string_value(item, "harness catalog entry.supported_wiring[]") or ""
                for item in raw_wiring
            ),
            requires_terminal=boolean_value(
                data["requires_terminal"], "harness catalog entry.requires_terminal"
            ),
            provider_ready=boolean_value(
                data["provider_ready"], "harness catalog entry.provider_ready"
            ),
            launch_available=boolean_value(
                data["launch_available"], "harness catalog entry.launch_available"
            ),
            reason=reason,
            detail=detail,
            extra=extras(data, {"name", *booleans, "supported_wiring", "reason", "detail"}),
        )

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "name": self.name,
                "installed": self.installed,
                "compatible": self.compatible,
                "supported_wiring": list(self.supported_wiring),
                "requires_terminal": self.requires_terminal,
                "provider_ready": self.provider_ready,
                "launch_available": self.launch_available,
                "reason": self.reason,
                "detail": self.detail,
            },
            self.extra,
        )


__all__ = ["CatalogEntry", "HarnessCatalogEntry"]
