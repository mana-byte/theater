"""Welcome/dashboard chrome driven solely by the public catalog."""

from __future__ import annotations

from collections.abc import Iterable

from textual.widgets import Static

from regie.dashboard.content import dashboard_sentence
from theater.frontend.dto.catalogs import HarnessCatalogEntry


class WelcomeDashboard(Static):
    """Show public launch readiness and the keyboard routes into operator workflows."""

    def __init__(
        self,
        *,
        sentences: list[str] | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self._sentence = dashboard_sentence(sentences, 0)

    def show_catalog(self, harnesses: Iterable[HarnessCatalogEntry]) -> None:
        lines = [
            "Régie",
            self._sentence,
            "Enter stage · h trajectory · Ctrl+P palette · q return safely",
        ]
        for harness in harnesses:
            state = "available" if harness.launch_available else (harness.reason or "unavailable")
            lines.append(f"{harness.name}: {state}")
        self.update("\n".join(lines))


__all__ = ["WelcomeDashboard"]
