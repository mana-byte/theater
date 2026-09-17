"""Terminal-provider public projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from theater.frontend.dto._wire import (
    JSONValue,
    append_extras,
    extras,
    freeze_json,
    integer_value,
    object_value,
    string_value,
    thaw_json,
)


@dataclass(frozen=True, slots=True)
class Provider:
    provider_id: str
    selector: str
    kind: str
    generation: int
    health: str
    capabilities: tuple[str, ...] = ()
    limits: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))
    last_report_revision: int | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> Provider:
        data = object_value(value, "provider")
        raw_capabilities = data.get("capabilities", [])
        if not isinstance(raw_capabilities, (list, tuple)):
            raise TypeError("provider.capabilities must be an array")
        capabilities = tuple(
            string_value(item, "provider.capabilities[]") or "" for item in raw_capabilities
        )
        limits = freeze_json(object_value(data.get("limits", {}), "provider.limits"))
        assert isinstance(limits, Mapping)
        revision = data.get("last_report_revision")
        if revision is not None:
            revision = integer_value(revision, "provider.last_report_revision")
        return cls(
            provider_id=string_value(data.get("provider_id"), "provider.provider_id") or "",
            selector=string_value(data.get("selector"), "provider.selector") or "",
            kind=string_value(data.get("kind"), "provider.kind") or "",
            generation=integer_value(data.get("generation"), "provider.generation"),
            health=string_value(data.get("health"), "provider.health") or "",
            capabilities=capabilities,
            limits=limits,
            last_report_revision=revision,
            extra=extras(
                data,
                {
                    "provider_id",
                    "selector",
                    "kind",
                    "generation",
                    "health",
                    "capabilities",
                    "limits",
                    "last_report_revision",
                },
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "provider_id": self.provider_id,
                "selector": self.selector,
                "kind": self.kind,
                "generation": self.generation,
                "health": self.health,
                "capabilities": list(self.capabilities),
                "limits": thaw_json(self.limits),
                "last_report_revision": self.last_report_revision,
            },
            self.extra,
        )


__all__ = ["Provider"]
