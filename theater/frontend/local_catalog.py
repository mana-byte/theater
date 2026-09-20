"""Optional local catalog fallback for disconnected frontend presentation."""

from __future__ import annotations

from theater.frontend.dto.catalogs import HarnessCatalogEntry


def _configured_registry_rows() -> list[dict]:
    from theater.harness.registry.install import install_configured
    from theater.harness.registry.lookup import describe

    install_configured()
    return describe()


def local_harness_catalog() -> tuple[HarnessCatalogEntry, ...]:
    """Describe the local registry without starting or contacting the daemon."""
    entries: list[HarnessCatalogEntry] = []
    for row in _configured_registry_rows():
        installed = row.get("installed") is True
        binary = row.get("binary")
        error = row.get("error")
        compatible = installed and isinstance(binary, str) and bool(binary) and not error
        entries.append(
            HarnessCatalogEntry.from_wire(
                {
                    "name": row.get("name"),
                    "installed": installed,
                    "compatible": compatible,
                    "supported_wiring": ["legacy"] if compatible else [],
                    "requires_terminal": compatible,
                    "provider_ready": True,
                    "launch_available": compatible,
                    "reason": None if compatible else "not_installed",
                    "detail": error or (None if compatible else "executable was not found"),
                    "approvals": row.get("approvals"),
                    "binary": binary or None,
                    "binaries": row.get("binaries", ()),
                    "icon": row.get("icon"),
                    "native_compatibility": row.get("native_compatibility"),
                }
            )
        )
    return tuple(entries)


__all__ = ["local_harness_catalog"]
