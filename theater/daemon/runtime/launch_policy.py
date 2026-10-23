"""Persisted launch-policy facts; callers retain fallback and admission decisions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from theater.harness.contracts.runtime import RuntimeHost


@dataclass(frozen=True, slots=True)
class LaunchPolicyFacts:
    fields: Mapping[str, object]

    @property
    def runtime_host_present(self) -> bool:
        return "runtime_host" in self.fields

    @property
    def runtime_host(self) -> RuntimeHost | None:
        value = self.fields.get("runtime_host")
        if isinstance(value, str):
            try:
                return RuntimeHost(value)
            except ValueError:
                pass
        return None

    @property
    def control_routes_present(self) -> bool:
        return "control_routes" in self.fields

    @property
    def control_routes(self) -> Mapping[str, object] | None:
        value = self.fields.get("control_routes")
        return value if isinstance(value, Mapping) else None


def decode_launch_policy(raw: str | None) -> LaunchPolicyFacts:
    """Read historical JSON without applying the stricter persistence-write validation."""
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        value = {}
    return LaunchPolicyFacts(value if isinstance(value, dict) else {})
