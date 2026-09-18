"""Shared public action projection over cached control-route facts."""

from theater.harness.contracts.runtime import CapabilityUnavailableReason, RuntimeCapability


def _capability_support(route, capability: RuntimeCapability) -> tuple[bool, str | None]:
    if getattr(route, "is_native", False):
        capabilities = getattr(route, "native_capabilities", None)
        effective = (
            RuntimeCapability.SEND if capability is RuntimeCapability.QUEUE_FOLLOWUP else capability
        )
        if capabilities is None:
            return False, CapabilityUnavailableReason.NOT_DETERMINED.value
        if capabilities.supports(effective):
            return True, None
        reason = capabilities.reason_for(effective)
        return False, None if reason is None else reason.value
    if getattr(route, "transport", None) is not None:
        return True, None
    reason = getattr(route, "unavailable_reason", None)
    return False, None if reason is None else reason.value


def project_control_action(
    route,
    capability: RuntimeCapability,
    *,
    route_available: bool,
    alive: bool,
    presence: str,
    presence_detail: str | None = None,
) -> dict[str, object]:
    """Build one public action projection from exact cached capability facts."""
    supported, unavailable_reason = _capability_support(route, capability)
    admissible = alive and supported and route_available and presence == "absent"
    reason: str | None = None
    detail: str | None = None
    if not alive:
        reason = "not_addressable"
        detail = "the participant is dead"
    elif not supported:
        reason = unavailable_reason or "unsupported"
    elif not route_available:
        reason = "route_unavailable"
    elif presence != "absent":
        reason = "human_present" if presence == "present" else "presence_unknown"
        detail = presence_detail
    return {
        "supported": supported,
        "route_available": route_available,
        "admissible": admissible,
        "reason": reason,
        "detail": detail,
    }


__all__ = ["project_control_action"]
