"""Version and response-envelope values for public clients."""

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
    integer_value,
    object_value,
    string_value,
    thaw_json,
)
from theater.frontend.errors import ErrorValue


@dataclass(frozen=True, slots=True)
class ApiVersion:
    major: int
    minor: int

    @classmethod
    def from_wire(cls, value: object) -> ApiVersion:
        data = object_value(value, "API version")
        return cls(
            major=integer_value(data.get("major"), "API version.major"),
            minor=integer_value(data.get("minor"), "API version.minor"),
        )


@dataclass(frozen=True, slots=True)
class HandshakeResult:
    api: ApiVersion
    daemon_instance_id: str
    package_version: str
    capabilities: tuple[str, ...]
    limits: Mapping[str, JSONValue]
    provider_generation: int | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> HandshakeResult:
        data = object_value(value, "handshake result")
        raw_capabilities = data.get("capabilities")
        if not isinstance(raw_capabilities, (list, tuple)):
            raise TypeError("handshake result.capabilities must be an array")
        limits = freeze_json(object_value(data.get("limits"), "handshake result.limits"))
        assert isinstance(limits, Mapping)
        generation = data.get("provider_generation")
        if generation is not None:
            generation = integer_value(generation, "handshake result.provider_generation")
        return cls(
            api=ApiVersion.from_wire(data.get("api")),
            daemon_instance_id=string_value(
                data.get("daemon_instance_id"), "handshake result.daemon_instance_id"
            )
            or "",
            package_version=string_value(
                data.get("package_version"), "handshake result.package_version"
            )
            or "",
            capabilities=tuple(
                string_value(item, "handshake result.capabilities[]") or ""
                for item in raw_capabilities
            ),
            limits=limits,
            provider_generation=generation,
            extra=extras(
                data,
                {
                    "api",
                    "daemon_instance_id",
                    "package_version",
                    "capabilities",
                    "limits",
                    "provider_generation",
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class Response:
    request_id: int
    ok: bool
    result: JSONValue = None
    error: ErrorValue | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> Response:
        data = object_value(value, "response")
        ok = boolean_value(data.get("ok"), "response.ok")
        has_result = "result" in data
        has_error = "error" in data
        if has_result == has_error or has_result != ok:
            raise ValueError("response must contain exactly one result or error matching ok")
        return cls(
            request_id=integer_value(data.get("id"), "response.id"),
            ok=ok,
            result=freeze_json(data.get("result"), "response.result") if ok else None,
            error=ErrorValue.from_wire(data.get("error")) if not ok else None,
            extra=extras(data, {"id", "ok", "result", "error"}),
        )

    def to_wire(self) -> dict[str, object]:
        body: dict[str, object] = {"id": self.request_id, "ok": self.ok}
        if self.ok:
            body["result"] = thaw_json(self.result)
        elif self.error is not None:
            body["error"] = self.error.to_wire()
        return append_extras(body, self.extra)


__all__ = ["ApiVersion", "HandshakeResult", "Response"]
