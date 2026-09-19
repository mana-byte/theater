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
class NativeCompatibility:
    """Daemon-reported native integration qualification for one harness."""

    status: str
    installed_version: str | None = None
    qualified_range: str | None = None
    policy: str | None = None
    reason: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> NativeCompatibility:
        data = object_value(value, "native compatibility")
        return cls(
            status=string_value(data.get("status"), "native compatibility.status") or "",
            installed_version=string_value(
                data.get("installed_version"),
                "native compatibility.installed_version",
                optional=True,
            ),
            qualified_range=string_value(
                data.get("qualified_range"),
                "native compatibility.qualified_range",
                optional=True,
            ),
            policy=string_value(data.get("policy"), "native compatibility.policy", optional=True),
            reason=string_value(data.get("reason"), "native compatibility.reason", optional=True),
            extra=extras(
                data,
                {"status", "installed_version", "qualified_range", "policy", "reason"},
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "status": self.status,
                "installed_version": self.installed_version,
                "qualified_range": self.qualified_range,
                "policy": self.policy,
                "reason": self.reason,
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
    approvals: tuple[str, ...] | None = None
    binary: str | None = None
    binaries: tuple[str, ...] = ()
    icon: str | None = None
    native_compatibility: NativeCompatibility | None = None
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
        raw_approvals = data.get("approvals")
        raw_binaries = data.get("binaries", ())
        raw_compatibility = data.get("native_compatibility")
        if reason is not None and not isinstance(reason, str):
            raise TypeError("harness catalog entry.reason must be a string or null")
        if detail is not None and not isinstance(detail, str):
            raise TypeError("harness catalog entry.detail must be a string or null")
        if raw_approvals is not None and not isinstance(raw_approvals, (list, tuple)):
            raise TypeError("harness catalog entry.approvals must be an array or null")
        if not isinstance(raw_binaries, (list, tuple)):
            raise TypeError("harness catalog entry.binaries must be an array")
        approvals = (
            None
            if raw_approvals is None
            else tuple(
                string_value(item, "harness catalog entry.approvals[]") or ""
                for item in raw_approvals
            )
        )
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
            approvals=approvals,
            binary=string_value(data.get("binary"), "harness catalog entry.binary", optional=True),
            binaries=tuple(
                string_value(item, "harness catalog entry.binaries[]") or ""
                for item in raw_binaries
            ),
            icon=string_value(data.get("icon"), "harness catalog entry.icon", optional=True),
            native_compatibility=(
                NativeCompatibility.from_wire(raw_compatibility)
                if raw_compatibility is not None
                else None
            ),
            extra=extras(
                data,
                {
                    "name",
                    *booleans,
                    "supported_wiring",
                    "reason",
                    "detail",
                    "approvals",
                    "binary",
                    "binaries",
                    "icon",
                    "native_compatibility",
                },
            ),
        )

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "installed": self.installed,
            "compatible": self.compatible,
            "supported_wiring": list(self.supported_wiring),
            "requires_terminal": self.requires_terminal,
            "provider_ready": self.provider_ready,
            "launch_available": self.launch_available,
            "reason": self.reason,
            "detail": self.detail,
            "binary": self.binary,
            "binaries": list(self.binaries),
            "icon": self.icon,
            "native_compatibility": (
                self.native_compatibility.to_wire()
                if self.native_compatibility is not None
                else None
            ),
        }
        if self.approvals is not None:
            result["approvals"] = list(self.approvals)
        return append_extras(result, self.extra)


__all__ = ["CatalogEntry", "HarnessCatalogEntry", "NativeCompatibility"]
