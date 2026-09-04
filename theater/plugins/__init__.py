"""Neutral foundations shared by Theater's package-manifest plugin kinds."""

from theater.plugins.loading import (
    LOCAL,
    MANIFEST_FILENAME,
    SHIPPED,
    PackageCandidate,
    cleanup_package,
    discover_packages,
    import_manifest,
    synthetic_package_name,
)
from theater.plugins.namespace import (
    NamespaceCollision,
    PluginNameReservation,
    reject_cross_kind_collisions,
)

__all__ = [
    "LOCAL",
    "MANIFEST_FILENAME",
    "SHIPPED",
    "NamespaceCollision",
    "PackageCandidate",
    "PluginNameReservation",
    "cleanup_package",
    "discover_packages",
    "import_manifest",
    "reject_cross_kind_collisions",
    "synthetic_package_name",
]
