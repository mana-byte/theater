"""Structured ranges within bounded span-detail text."""

from __future__ import annotations

from dataclasses import dataclass

from theater.trajectory import ContentFormat


@dataclass(frozen=True, slots=True)
class DetailBlock:
    start_line: int
    end_line: int
    format: ContentFormat
    label: str | None = None

    def clipped(self, line_count: int) -> DetailBlock | None:
        if self.start_line >= line_count:
            return None
        return DetailBlock(
            self.start_line,
            min(self.end_line, line_count),
            self.format,
            self.label,
        )


__all__ = ["DetailBlock"]
