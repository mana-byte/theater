"""Bounded canonical content values."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Self

from theater.constants.trajectory import (
    TRAJECTORY_DETAIL_FIELD_MAX_BYTES,
    TRAJECTORY_DETAIL_NAME_MAX_BYTES,
    TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
    TRAJECTORY_MAX_DETAILS_PER_RECORD,
)
from theater.trajectory.enums import ContentFormat, TrajectoryValidationError
from theater.trajectory.validation import enum_value, integer, keys, mapping, string

_OMISSION_MARKER = re.compile(r"… \d+ bytes omitted …")
DisplayUnit = tuple[str, int]


def sanitize_text(value: str) -> str:
    """Validate UTF-8 and make terminal controls visible without Rich escaping."""
    if not isinstance(value, str):
        raise TrajectoryValidationError("trajectory text must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TrajectoryValidationError("trajectory text must contain valid UTF-8") from exc
    return "".join(
        f"\\x{ord(char):02x}"
        if (ord(char) < 0x20 and char not in "\n\r\t") or 0x7F <= ord(char) <= 0x9F
        else char
        for char in value
    )


def escape_rich_text(value: str) -> str:
    """Compatibility helper that sanitizes controls but leaves markup literal."""
    return sanitize_text(value)


def _control_safe(value: str) -> bool:
    return all(
        not ((ord(char) < 0x20 and char not in "\n\r\t") or 0x7F <= ord(char) <= 0x9F)
        for char in value
    )


def _identifier_safe(value: str) -> bool:
    return all(not (ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F) for char in value)


def _sanitized_char(char: str) -> str:
    codepoint = ord(char)
    if (codepoint < 0x20 and char not in "\n\r\t") or 0x7F <= codepoint <= 0x9F:
        return f"\\x{codepoint:02x}"
    return char


def _display_units(value: str) -> tuple[DisplayUnit, ...]:
    return tuple((_sanitized_char(char), len(char.encode("utf-8"))) for char in value)


def _take_display_prefix(
    units: tuple[DisplayUnit, ...], budget: int
) -> tuple[str, int, int, tuple[DisplayUnit, ...]]:
    selected: list[str] = []
    selected_units: list[DisplayUnit] = []
    source_bytes = 0
    display_bytes = 0
    for index, (displayed, original_bytes) in enumerate(units):
        displayed_bytes = len(displayed.encode("utf-8"))
        if display_bytes + displayed_bytes > budget:
            return "".join(selected), index, source_bytes, tuple(selected_units)
        selected.append(displayed)
        selected_units.append((displayed, original_bytes))
        source_bytes += original_bytes
        display_bytes += displayed_bytes
    return "".join(selected), len(units), source_bytes, tuple(selected_units)


def _take_display_suffix(
    units: tuple[DisplayUnit, ...], budget: int, lower_bound: int
) -> tuple[str, int, int, tuple[DisplayUnit, ...]]:
    selected: list[DisplayUnit] = []
    source_bytes = 0
    display_bytes = 0
    start = len(units)
    for index in range(len(units) - 1, lower_bound - 1, -1):
        displayed, original_bytes = units[index]
        displayed_bytes = len(displayed.encode("utf-8"))
        if display_bytes + displayed_bytes > budget:
            break
        selected.append((displayed, original_bytes))
        source_bytes += original_bytes
        display_bytes += displayed_bytes
        start = index
    selected.reverse()
    return "".join(displayed for displayed, _ in selected), start, source_bytes, tuple(selected)


def _clip_units(
    units: tuple[DisplayUnit, ...], max_bytes: int, prior_omitted: int = 0
) -> tuple[str, int, tuple[DisplayUnit, ...]]:
    total = prior_omitted + sum(original_bytes for _, original_bytes in units)
    display_bytes = sum(len(displayed.encode("utf-8")) for displayed, _ in units)
    if prior_omitted == 0 and display_bytes <= max_bytes:
        return "".join(displayed for displayed, _ in units), 0, units
    marker = f"… {total} bytes omitted …"
    for _ in range(12):
        marker_bytes = len(marker.encode("utf-8"))
        if marker_bytes >= max_bytes:
            return "", total, ()
        available = max_bytes - marker_bytes
        head_budget = available // 2
        head, head_end, head_source_bytes, head_units = _take_display_prefix(units, head_budget)
        tail, _tail_start, tail_source_bytes, tail_units = _take_display_suffix(
            units, available - len(head.encode("utf-8")), head_end
        )
        omitted = total - head_source_bytes - tail_source_bytes
        next_marker = f"… {omitted} bytes omitted …"
        result = head + next_marker + tail
        if next_marker == marker and len(result.encode("utf-8")) <= max_bytes:
            return result, omitted, (*head_units, (next_marker, 0), *tail_units)
        marker = next_marker
    marker_bytes = len(marker.encode("utf-8"))
    if marker_bytes > max_bytes:
        return "", total, ()
    return marker, total, ((marker, 0),)


def _clip_text(value: str, max_bytes: int) -> tuple[str, int, tuple[DisplayUnit, ...]]:
    return _clip_units(_display_units(value), max_bytes)


def bounded_text(value: str, *, max_bytes: int, label: str, nonempty: bool = False) -> str:
    if not isinstance(value, str):
        raise TrajectoryValidationError(f"{label} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TrajectoryValidationError(f"{label} must contain valid UTF-8") from exc
    if not _identifier_safe(value):
        raise TrajectoryValidationError(f"{label} must not contain control characters")
    if nonempty and not value:
        raise TrajectoryValidationError(f"{label} must be a non-empty string")
    if len(encoded) > max_bytes:
        raise TrajectoryValidationError(f"{label} exceeds {max_bytes} encoded bytes")
    return value


@dataclass(frozen=True, slots=True)
class ContentPreview:
    text: str
    omitted_bytes: int = 0
    _units: tuple[DisplayUnit, ...] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TrajectoryValidationError("content preview text must be a string")
        if type(self.omitted_bytes) is not int or self.omitted_bytes < 0:
            raise TrajectoryValidationError(
                "content preview omitted_bytes must be a non-negative integer"
            )
        try:
            encoded = self.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TrajectoryValidationError(
                "content preview text must contain valid UTF-8"
            ) from exc
        if not _control_safe(self.text):
            raise TrajectoryValidationError("content preview text must be control safe")
        if len(encoded) > TRAJECTORY_DETAIL_FIELD_MAX_BYTES:
            raise TrajectoryValidationError(
                f"content preview exceeds {TRAJECTORY_DETAIL_FIELD_MAX_BYTES} encoded bytes"
            )
        if self._units is not None:
            if "".join(displayed for displayed, _ in self._units) != self.text:
                raise TrajectoryValidationError("content preview source units do not match text")
            if any(
                type(source_bytes) is not int or source_bytes < 0 for _, source_bytes in self._units
            ):
                raise TrajectoryValidationError("content preview source units are invalid")

    @classmethod
    def from_text(cls, value: str, *, max_bytes: int = TRAJECTORY_DETAIL_FIELD_MAX_BYTES) -> Self:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise TrajectoryValidationError("content preview max_bytes must be a positive integer")
        if not isinstance(value, str):
            raise TrajectoryValidationError("trajectory text must be a string")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TrajectoryValidationError("trajectory text must contain valid UTF-8") from exc
        text, omitted, units = _clip_text(value, min(max_bytes, TRAJECTORY_DETAIL_FIELD_MAX_BYTES))
        return cls(text=text, omitted_bytes=omitted, _units=units)

    @property
    def encoded_bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    def to_wire(self) -> dict[str, object]:
        return {"text": self.text, "omitted_bytes": self.omitted_bytes}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "content preview")
        keys(data, required={"text", "omitted_bytes"}, optional=set(), label="content preview")
        return cls(
            text=string(data["text"], "content preview.text"),
            omitted_bytes=integer(data["omitted_bytes"], "content preview.omitted_bytes"),
        )


@dataclass(frozen=True, slots=True)
class DetailField:
    name: str
    value: ContentPreview | str
    format: ContentFormat = ContentFormat.TEXT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "name",
            bounded_text(
                self.name,
                max_bytes=TRAJECTORY_DETAIL_NAME_MAX_BYTES,
                label="detail field name",
                nonempty=True,
            ),
        )
        if isinstance(self.value, str):
            object.__setattr__(self, "value", ContentPreview.from_text(self.value))
        elif not isinstance(self.value, ContentPreview):
            raise TrajectoryValidationError("detail field value must be ContentPreview or string")
        object.__setattr__(
            self, "format", enum_value(ContentFormat, self.format, "detail field.format")
        )

    @classmethod
    def from_text(
        cls,
        name: str,
        value: str,
        *,
        format: ContentFormat = ContentFormat.TEXT,
    ) -> Self:
        return cls(name=name, value=ContentPreview.from_text(value), format=format)

    @property
    def preview(self) -> ContentPreview:
        assert isinstance(self.value, ContentPreview)
        return self.value

    def to_wire(self) -> dict[str, object]:
        return {"name": self.name, "format": self.format.value, "value": self.preview.to_wire()}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "detail field")
        keys(data, required={"name", "format", "value"}, optional=set(), label="detail field")
        return cls(
            name=string(data["name"], "detail field.name"),
            format=enum_value(ContentFormat, data["format"], "detail field.format"),
            value=ContentPreview.from_wire(data["value"]),
        )


def bound_detail_fields(fields: Iterable[DetailField]) -> tuple[DetailField, ...]:
    """Apply per-field and aggregate byte limits in input order."""
    bounded: list[DetailField] = []
    remaining = TRAJECTORY_DETAIL_RECORD_MAX_BYTES
    for index, field_value in enumerate(fields):
        if index >= TRAJECTORY_MAX_DETAILS_PER_RECORD:
            break
        if not isinstance(field_value, DetailField):
            raise TrajectoryValidationError("record details must contain DetailField values")
        preview = field_value.preview
        name_bytes = len(field_value.name.encode("utf-8"))
        value_budget = remaining - name_bytes
        if value_budget <= 0:
            break
        if preview.encoded_bytes <= min(value_budget, TRAJECTORY_DETAIL_FIELD_MAX_BYTES):
            clipped = preview
        else:
            clipped = _rebound_preview(
                preview, max_bytes=min(value_budget, TRAJECTORY_DETAIL_FIELD_MAX_BYTES)
            )
        if not clipped.text and preview.text:
            break
        bounded.append(DetailField(field_value.name, clipped, field_value.format))
        remaining -= name_bytes + clipped.encoded_bytes
        if remaining <= 0:
            break
    return tuple(bounded)


def _rebound_preview(preview: ContentPreview, *, max_bytes: int) -> ContentPreview:
    if preview.encoded_bytes <= max_bytes:
        return preview
    units = preview._units
    if units is None:
        text = (
            _OMISSION_MARKER.sub("", preview.text, count=1)
            if preview.omitted_bytes
            else preview.text
        )
        units = _display_units(text)
    else:
        units = tuple(unit for unit in units if unit[1] > 0)
    text, omitted, retained_units = _clip_units(units, max_bytes, preview.omitted_bytes)
    return ContentPreview(text=text, omitted_bytes=omitted, _units=retained_units)


__all__ = [
    "ContentPreview",
    "DetailField",
    "bound_detail_fields",
    "bounded_text",
    "escape_rich_text",
    "sanitize_text",
]
