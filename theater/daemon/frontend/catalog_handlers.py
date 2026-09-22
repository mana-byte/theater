"""Public adapters for daemon-owned harness, model, and skill catalogs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.rpc.skills import _skills_list, _skills_load
from theater.daemon.rpc.spawning import _harnesses, _models
from theater.frontend.capabilities import METHOD_CATALOG, TERMINAL_PROVIDER_CAPABILITY
from theater.frontend.schemas import validator_for
from theater.models import ProviderRecord

_DEFAULT_PROVIDER_SELECTOR = "tmux"


@dataclass(frozen=True, slots=True)
class _ProviderReadiness:
    requested: str
    record: ProviderRecord | None
    health: str | None
    ready: bool
    reason: str | None
    detail: str | None

    def to_wire(self) -> dict[str, object]:
        record = self.record
        return {
            "requested": self.requested,
            "provider_id": None if record is None else record.provider_id,
            "selector": None if record is None else record.selector,
            "kind": None if record is None else record.kind,
            "health": self.health,
            "ready": self.ready,
            "reason": self.reason,
            "detail": self.detail,
        }


def _bounded_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def _selected_provider(daemon, requested: object) -> _ProviderReadiness:
    selector = _DEFAULT_PROVIDER_SELECTOR if requested is None else str(requested)
    record = daemon.store.providers.get(selector)
    if record is None:
        record = daemon.store.providers.get_by_selector(selector)
    if record is None:
        return _ProviderReadiness(
            requested=selector,
            record=None,
            health=None,
            ready=False,
            reason="provider_unavailable",
            detail=f"no provider matches {selector!r}",
        )
    terminal_service = getattr(daemon, "terminal_service", None)
    connections = getattr(terminal_service, "connections", None)
    health_for = getattr(connections, "health", None)
    health = health_for(record.provider_id) if callable(health_for) else "offline"
    if TERMINAL_PROVIDER_CAPABILITY not in record.capabilities:
        return _ProviderReadiness(
            requested=selector,
            record=record,
            health=health,
            ready=False,
            reason="provider_incompatible",
            detail=(
                f"provider {record.selector!r} does not advertise {TERMINAL_PROVIDER_CAPABILITY!r}"
            ),
        )

    if health != "online":
        return _ProviderReadiness(
            requested=selector,
            record=record,
            health=health,
            ready=False,
            reason="provider_unavailable",
            detail=f"provider {record.selector!r} is {health}",
        )
    return _ProviderReadiness(
        requested=selector,
        record=record,
        health=health,
        ready=True,
        reason=None,
        detail=None,
    )


def _harness_entry(row: Mapping[str, object], provider: _ProviderReadiness) -> dict[str, object]:
    result = dict(row)
    installed = row.get("installed") is True
    error = _bounded_text(row.get("error"), 4096)
    supports_legacy = bool(_bounded_text(row.get("binary"), 512)) and error is None
    compatible = installed and supports_legacy
    reason: str | None
    detail: str | None
    if not installed:
        reason, detail = "not_installed", error or "the harness executable was not found"
    elif not supports_legacy:
        reason, detail = (
            "harness_incompatible",
            error or "the harness has no supported launch wiring",
        )
    elif not provider.ready:
        reason, detail = provider.reason, provider.detail
    else:
        reason, detail = None, None
    result.update(
        {
            "installed": installed,
            "compatible": compatible,
            # Public legacy launch is provider-backed. Native launch remains an
            # internal capability until its provider launch path is complete.
            "supported_wiring": ["legacy"] if supports_legacy else [],
            "requires_terminal": supports_legacy,
            "provider_ready": provider.ready,
            "launch_available": compatible and provider.ready,
            "reason": reason,
            "detail": detail,
            "provider": provider.to_wire(),
        }
    )
    return result


def _validated(method: str, result: dict[str, object]) -> dict[str, object]:
    validator_for(METHOD_CATALOG[method].result_schema_id).validate(result)
    return result


async def catalogs_harnesses(daemon, _context: ConnectionContext, params: dict) -> dict:
    provider = _selected_provider(daemon, params.get("provider"))
    rows = await _harnesses(daemon, {})
    return _validated(
        "frontend.catalogs.harnesses",
        {
            "items": [_harness_entry(row, provider) for row in rows],
            "next_cursor": None,
            "provider": provider.to_wire(),
        },
    )


async def catalogs_models(daemon, _context: ConnectionContext, params: dict) -> dict:
    provider = _selected_provider(daemon, params.get("provider"))
    rows = await _models(daemon, {})
    return _validated(
        "frontend.catalogs.models",
        {
            "items": rows,
            "next_cursor": None,
            "provider": provider.to_wire(),
        },
    )


async def skills_list(daemon, _context: ConnectionContext, _params: dict) -> dict:
    snapshot = await _skills_list(daemon, {})
    return _validated(
        "frontend.skills.list",
        {
            "items": snapshot["skills"],
            "next_cursor": None,
            "rejections": snapshot["rejections"],
        },
    )


async def skills_load(daemon, _context: ConnectionContext, params: dict) -> dict:
    return _validated("frontend.skills.load", await _skills_load(daemon, {"name": params["name"]}))


CATALOG_HANDLERS = MappingProxyType(
    {
        "frontend.catalogs.harnesses": catalogs_harnesses,
        "frontend.catalogs.models": catalogs_models,
        "frontend.skills.list": skills_list,
        "frontend.skills.load": skills_load,
    }
)

__all__ = ["CATALOG_HANDLERS"]
