"""A bounded read-only summary of public usage values."""

from __future__ import annotations

from collections.abc import Mapping

from regie.render import bounded_text
from regie.widgets.chrome import NonSelectableStatic


class UsageBreakdown(NonSelectableStatic):
    """Keep additive usage summary fields visible without local accounting logic."""

    def show_summary(self, summary: Mapping[str, object]) -> None:
        if summary:
            text = " · ".join(f"{key}={value}" for key, value in summary.items())
            self.update(bounded_text(text))
        else:
            self.update("")


__all__ = ["UsageBreakdown"]
