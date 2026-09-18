"""Small reusable trajectory chrome for the standalone public surface."""

from __future__ import annotations

from regie.widgets.chrome import NonSelectableStatic


class TrajectoryFooter(NonSelectableStatic):
    """Keep navigation instructions visible without importing the old TUI tree."""

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            "j/k navigate · Esc return · H/L older/newer",
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )


__all__ = ["TrajectoryFooter"]
