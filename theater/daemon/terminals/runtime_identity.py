"""Provider runtime identity evidence, including the legacy tmux wire shape."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

RUNTIME_IDENTITY_CAPABILITY = "terminal-runtime-identity.v1"
_IDENTITY_MAX_CHARS = 4096


class RuntimeIdentityInvalid(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """An identity and its evidence shape; dual evidence has generic provenance."""

    value: str
    legacy: bool
    dual: bool = False


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _IDENTITY_MAX_CHARS:
        raise RuntimeIdentityInvalid(f"{field} must be a non-empty bounded string")
    return value


def stored_identity(evidence: Mapping[str, object], provider_kind: str) -> RuntimeIdentity | None:
    """Read durable occupant evidence without inferring an epoch from missing keys."""
    generic = (
        _identity(evidence["runtime_identity"], "occupant.runtime_identity")
        if "runtime_identity" in evidence
        else None
    )
    legacy = None
    if "tmux_server_identity" in evidence:
        if provider_kind != "tmux" or evidence.get("provider_kind") != "tmux":
            raise RuntimeIdentityInvalid(
                "legacy tmux occupant evidence has the wrong provider kind"
            )
        legacy = _identity(evidence["tmux_server_identity"], "occupant.tmux_server_identity")
    if generic is not None and legacy is not None and generic != legacy:
        raise RuntimeIdentityInvalid("terminal occupant runtime identities disagree")
    if generic is not None:
        return RuntimeIdentity(generic, legacy=False, dual=legacy is not None)
    if legacy is not None:
        return RuntimeIdentity(legacy, legacy=True)
    return None


def binding_identity_duty(
    evidence: Iterable[Mapping[str, object]], provider_kind: str
) -> tuple[bool, bool]:
    """Return retained generic and legacy duties from durable binding evidence."""
    generic = False
    legacy = False
    for occupant in evidence:
        identity = stored_identity(occupant, provider_kind)
        if identity is None:
            continue
        generic |= not identity.legacy
        legacy |= identity.legacy or identity.dual
    return generic, legacy


def report_identity(
    provider_kind: str,
    capabilities: Sequence[str],
    facts: Mapping[str, object] | None,
    terminals: Sequence[Mapping[str, object]],
    *,
    complete: bool,
    retained_generic: bool,
    retained_legacy: bool,
) -> RuntimeIdentity | None:
    """Require every identity a complete report owes, then check its terminal evidence."""
    declared = RUNTIME_IDENTITY_CAPABILITY in capabilities
    generic_allowed = declared or retained_generic
    generic_required = generic_allowed and complete
    legacy_required = (provider_kind == "tmux" or retained_legacy) and complete
    data: Mapping[str, object] = facts or {}
    has_generic = "runtime_identity" in data
    has_legacy = "tmux_server_identity" in data
    if has_generic and not generic_allowed:
        raise RuntimeIdentityInvalid(
            "facts.runtime_identity requires terminal-runtime-identity.v1 or retained identity"
        )
    if has_legacy and provider_kind != "tmux":
        raise RuntimeIdentityInvalid("only tmux providers may report facts.tmux_server_identity")
    if generic_required and not has_generic:
        raise RuntimeIdentityInvalid("a complete provider report requires facts.runtime_identity")
    if legacy_required and not has_legacy:
        raise RuntimeIdentityInvalid(
            "a complete tmux provider report requires facts.tmux_server_identity"
        )
    generic = _identity(data["runtime_identity"], "facts.runtime_identity") if has_generic else None
    legacy = (
        _identity(data["tmux_server_identity"], "facts.tmux_server_identity")
        if has_legacy
        else None
    )
    if generic is not None and legacy is not None and generic != legacy:
        raise RuntimeIdentityInvalid("provider report runtime identities disagree")
    _check_terminal_occupants(terminals, provider_kind, generic_allowed, generic, legacy)
    if generic is not None:
        return RuntimeIdentity(generic, legacy=False, dual=legacy is not None)
    if legacy is not None:
        return RuntimeIdentity(legacy, legacy=True)
    return None


def _check_terminal_occupants(
    terminals: Sequence[Mapping[str, object]],
    provider_kind: str,
    generic_allowed: bool,
    generic: str | None,
    legacy: str | None,
) -> None:
    for terminal in terminals:
        occupant = terminal.get("occupant")
        if not isinstance(occupant, Mapping):
            raise RuntimeIdentityInvalid("terminal occupant evidence must be an object")
        if "runtime_identity" in occupant and not generic_allowed:
            raise RuntimeIdentityInvalid(
                "occupant.runtime_identity requires declared or retained identity"
            )
        stored_identity(occupant, provider_kind)
        if generic is not None and occupant.get("runtime_identity") != generic:
            raise RuntimeIdentityInvalid(
                "terminal occupant evidence does not match runtime identity"
            )
        if legacy is not None and (
            occupant.get("provider_kind") != "tmux"
            or occupant.get("tmux_server_identity") != legacy
        ):
            raise RuntimeIdentityInvalid(
                "tmux terminal occupant evidence does not match the reported server identity"
            )
