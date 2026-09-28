"""Package-manifest plugin loading, for frontends that take plugins the way Theater does.

A plugin is a directory holding ``manifest.py``; it is imported under an isolated synthetic
package, so its sibling modules never collide. Frontends use this, not ``theater.plugins``.
"""

from __future__ import annotations

from theater.plugins.loading import (
    LOCAL,
    MANIFEST_FILENAME,
    SHIPPED,
    PackageCandidate,
    cleanup_package,
    discover_packages,
    import_manifest,
)

__all__ = [
    "LOCAL",
    "MANIFEST_FILENAME",
    "SHIPPED",
    "PackageCandidate",
    "cleanup_package",
    "discover_packages",
    "import_manifest",
]
