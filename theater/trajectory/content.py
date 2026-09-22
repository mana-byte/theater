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
from theater.trajectory.clipping import (
    RetainedSource,
    clip_text,
    control_safe,
    display_text,
    sanitize_text,
)
from theater.trajectory.enums import ContentFormat, TrajectoryValidationError
from theater.trajectory.validation import enum_value, integer, keys, mapping, string

_OMISSION_MARKER = re.compile(r"… \d+ bytes omitted …")


def escape_rich_text(value: str) -> str:
    """Compatibility helper that sanitizes controls but leaves markup literal."""
    return sanitize_text(value)


def _identifier_safe(value: str) -> bool:
    return all(not (ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F) for char in value)


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
    _source: RetainedSource | None = field(default=None, repr=False, compare=False)

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
        if not control_safe(self.text):
            raise TrajectoryValidationError("content preview text must be control safe")
        if len(encoded) > TRAJECTORY_DETAIL_FIELD_MAX_BYTES:
            raise TrajectoryValidationError(
                f"content preview exceeds {TRAJECTORY_DETAIL_FIELD_MAX_BYTES} encoded bytes"
            )
        if self._source is not None:
            head, tail = self._source
            marker = f"… {self.omitted_bytes} bytes omitted …" if self.omitted_bytes else ""
            expected = display_text(head) + marker + display_text(tail)
            if expected != self.text and (self.text or head or tail):
                raise TrajectoryValidationError("content preview source does not match text")

    @classmethod
    def from_text(cls, value: str, *, max_bytes: int = TRAJECTORY_DETAIL_FIELD_MAX_BYTES) -> Self:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise TrajectoryValidationError("content preview max_bytes must be a positive integer")
        if not isinstance(value, str):
            raise TrajectoryValidationError("trajectory text must be a string")
        text, omitted, source = clip_text(value, min(max_bytes, TRAJECTORY_DETAIL_FIELD_MAX_BYTES))
        return cls(text=text, omitted_bytes=omitted, _source=source)

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
    if preview._source is None:
        source = (
            _OMISSION_MARKER.sub("", preview.text, count=1)
            if preview.omitted_bytes
            else preview.text
        )
    else:
        source = "".join(preview._source)
    text, omitted, retained = clip_text(source, max_bytes, preview.omitted_bytes)
    return ContentPreview(text=text, omitted_bytes=omitted, _source=retained)


__all__ = [
    "ContentPreview",
    "DetailField",
    "bound_detail_fields",
    "bounded_text",
    "escape_rich_text",
    "sanitize_text",
]
