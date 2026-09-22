"""Public API negotiation and immutable per-connection identity."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from theater import __version__
from theater.daemon.frontend.validation import PublicRequestError
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
    provider_connection_token: str | None = None


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
    provider_connection_token: str | None = None
    if role is ConnectionRole.PROVIDER:
        provider_id = str(params["provider_id"])
        credential = str(params["provider_credential"])
        try:
            provider_generation, provider_connection_token = (
                daemon.terminal_service.authenticate_handshake(
                    provider_id,
                    credential,
                    callback=channel is ConnectionChannel.CALLBACK,
                )
            )
        except PublicRequestError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "provider_unavailable")
            details = getattr(exc, "details", {"provider_id": provider_id})
            raise PublicRequestError(code, str(exc), details) from exc

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
        provider_connection_token=provider_connection_token,
    )
    limits = dict(PUBLIC_LIMITS)
    if provider_generation is not None and channel is ConnectionChannel.CALLBACK:
        limits.update(daemon.terminal_service.negotiated_limits(provider_id, provider_generation))
    result: dict[str, object] = {
        "api": {"major": PUBLIC_API_MAJOR, "minor": negotiated_minor},
        "daemon_instance_id": daemon_instance_id(daemon),
        "package_version": __version__,
        "capabilities": list(CAPABILITIES),
        "limits": limits,
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
