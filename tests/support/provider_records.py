"""Builders for terminal-provider records persisted through the daemon store."""

from __future__ import annotations

from collections.abc import Mapping

from theater.models import ProviderRecord, now


def provider_record(
    provider_id: str = "provider-a",
    *,
    selector: str = "tmux",
    kind: str = "fixture",
    credential_verifier: str = "a" * 64,
    capabilities: tuple[str, ...] = ("terminal-provider.v1",),
    limits: Mapping[str, object] | None = None,
    generation: int = 1,
    last_report_revision: int | None = 1,
) -> ProviderRecord:
    timestamp = now()
    return ProviderRecord(
        provider_id=provider_id,
        selector=selector,
        kind=kind,
        credential_verifier=credential_verifier,
        configuration_version=1,
        capabilities=capabilities,
        limits=dict(limits or {}),
        generation=generation,
        last_report_revision=last_report_revision,
        created_at=timestamp,
        updated_at=timestamp,
    )


def register_provider(daemon, provider_id: str = "provider-a", **fields) -> ProviderRecord:
    """Persist a provider record the way registration would, without a live connection."""
    record = provider_record(provider_id, **fields)
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(record, connection=unit.connection)
    return record


def online_provider(
    daemon, monkeypatch, provider_id: str = "provider-tmux", selector: str = "tmux"
) -> ProviderRecord:
    """Register a provider and make the daemon see its connection as current and online."""
    record = register_provider(daemon, provider_id, selector=selector)
    connections = daemon.terminal_service.connections
    monkeypatch.setattr(connections, "is_current", lambda *_: True)
    monkeypatch.setattr(connections, "health", lambda *_: "online")
    return record
