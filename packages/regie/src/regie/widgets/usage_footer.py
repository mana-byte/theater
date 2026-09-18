"""A compact public-usage footer, intentionally independent of private accounting."""

from __future__ import annotations

from collections.abc import Mapping

from regie.render import bounded_text
from regie.widgets.chrome import NonSelectableStatic


class UsageFooter(NonSelectableStatic):
    """Render only the bounded totals returned by the public usage facade."""

    def show_totals(self, totals: Mapping[str, object]) -> None:
        if not totals:
            self.update("usage unavailable")
            return
        self.update(bounded_text(" · ".join(f"{key}={value}" for key, value in totals.items())))


__all__ = ["UsageFooter"]
