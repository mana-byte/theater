"""A callable clock tests set by hand."""

from __future__ import annotations


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value
