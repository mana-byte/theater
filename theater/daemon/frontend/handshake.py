"""Public API negotiation and immutable per-connection identity."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass

from theater import __version__
from theater.daemon.frontend.validation import PublicRequestError
from theater.daemon.plugins.credentials import credential_verifier
from theater.frontend.capabilities import (
    CAPABILITIES,
    PUBLIC_API_MAJOR,
    PUBLIC_API_MINOR,
    PUBLIC_LIMITS,
    ConnectionChannel,
    ConnectionRole,
)

DAEMON_INSTANCE_ID_META_KEY = "frontend_daemon_instance_id"


@dataclass(frozen=True, slots=True)
class ConnectionContext:
    """Identity and negotiated contract permanently bound after handshake."""

    client_id: str
    role: ConnectionRole
    channel: ConnectionChannel
    api_major: int
    api_minor: int
    capabilities: frozenset[str]
    provider_id: str | None = None
    provider_generation: int | None = None


def daemon_instance_id(daemon) -> str:
    """Load or create the database-owned identity that survives daemon restart."""
    existing = daemon.store.get_meta(DAEMON_INSTANCE_ID_META_KEY)
    if existing:
        return existing
    value = f"daemon-{uuid.uuid4().hex}"
    daemon.store.set_meta(DAEMON_INSTANCE_ID_META_KEY, value)
    return value


def negotiate(daemon, params: dict[str, object]) -> tuple[ConnectionContext, dict[str, object]]:
    """Validate compatibility and return the bound context plus handshake result."""
    api = params["api"]
    assert isinstance(api, dict)
    major = api["major"]
    minor = api["minor"]
    assert isinstance(major, int) and isinstance(minor, int)
    if major != PUBLIC_API_MAJOR:
        raise PublicRequestError(
            "incompatible_api",
            f"public API major {major} is incompatible with server major {PUBLIC_API_MAJOR}",
            {"requested_major": major, "server_major": PUBLIC_API_MAJOR},
        )

    requested = params["required_capabilities"]
    assert isinstance(requested, list)
    missing = sorted(set(requested) - set(CAPABILITIES))
    if missing:
        raise PublicRequestError(
            "missing_capability",
            "the server does not provide every required capability",
            {"missing": missing},
        )

    role = ConnectionRole(str(params["role"]))
    channel = ConnectionChannel(str(params["channel"]))
    provider_id: str | None = None
    provider_generation: int | None = None
    if role is ConnectionRole.PROVIDER:
        provider_id = str(params["provider_id"])
        credential = str(params["provider_credential"])
        record = daemon.store.providers.get(provider_id)
        if record is None or not hmac.compare_digest(
            credential_verifier(credential), record.credential_verifier
        ):
            raise PublicRequestError(
                "provider_unavailable",
                "the provider identity or credential was not accepted",
                {"provider_id": provider_id},
            )
        provider_generation = record.generation

    negotiated_minor = min(minor, PUBLIC_API_MINOR)
    context = ConnectionContext(
        client_id=str(params["client_id"]),
        role=role,
        channel=channel,
        api_major=PUBLIC_API_MAJOR,
        api_minor=negotiated_minor,
        capabilities=frozenset(CAPABILITIES),
        provider_id=provider_id,
        provider_generation=provider_generation,
    )
    result: dict[str, object] = {
        "api": {"major": PUBLIC_API_MAJOR, "minor": negotiated_minor},
        "daemon_instance_id": daemon_instance_id(daemon),
        "package_version": __version__,
        "capabilities": list(CAPABILITIES),
        "limits": dict(PUBLIC_LIMITS),
    }
    if provider_generation is not None:
        result["provider_generation"] = provider_generation
    return context, result


__all__ = [
    "DAEMON_INSTANCE_ID_META_KEY",
    "ConnectionContext",
    "daemon_instance_id",
    "negotiate",
]
