"""Small dashboard surface driven entirely by the public harness catalog."""

from __future__ import annotations

from collections.abc import Iterable

from textual.widgets import Static

from theater.frontend.dto.catalogs import HarnessCatalogEntry


class CatalogDashboard(Static):
    """Show daemon-reported availability without local harness discovery."""

    def show_harnesses(self, harnesses: Iterable[HarnessCatalogEntry]) -> None:
        lines = ["Régie"]
        for harness in harnesses:
            status = "available" if harness.launch_available else (harness.reason or "unavailable")
            lines.append(f"{harness.name}: {status}")
        self.update("\n".join(lines))


__all__ = ["CatalogDashboard"]
