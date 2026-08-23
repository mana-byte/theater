"""Bounded canonical content values."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

from theater.constants.trajectory import (
    TRAJECTORY_DETAIL_FIELD_MAX_BYTES,
    TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
)
from theater.trajectory.enums import ContentFormat, TrajectoryValidationError
from theater.trajectory.validation import enum_value, integer, keys, mapping, string


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


def _fit_prefix(data: bytes, budget: int) -> str:
    return data[:budget].decode("utf-8", errors="ignore")


def _fit_suffix(data: bytes, budget: int) -> str:
    return data[-budget:].decode("utf-8", errors="ignore") if budget else ""


def _clip_text(value: str, max_bytes: int) -> tuple[str, int]:
    data = value.encode("utf-8")
    total = len(data)
    if total <= max_bytes:
        return value, 0
    omitted_guess = total - max_bytes
    for _ in range(8):
        marker = f"… {omitted_guess} bytes omitted …"
        marker_bytes = len(marker.encode("utf-8"))
        available = max_bytes - marker_bytes
        if available < 0:
            break
        head = _fit_prefix(data, available // 2)
        tail = _fit_suffix(data, available - len(head.encode("utf-8")))
        omitted = total - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
        marker = f"… {omitted} bytes omitted …"
        result = head + marker + tail
        if omitted == omitted_guess and len(result.encode("utf-8")) <= max_bytes:
            return result, omitted
        omitted_guess = omitted
    marker = "…"
    if len(marker.encode("utf-8")) > max_bytes:
        marker = ""
    return marker, total


@dataclass(frozen=True, slots=True)
class ContentPreview:
    text: str
    omitted_bytes: int = 0

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

    @classmethod
    def from_text(cls, value: str, *, max_bytes: int = TRAJECTORY_DETAIL_FIELD_MAX_BYTES) -> Self:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise TrajectoryValidationError("content preview max_bytes must be a positive integer")
        safe = sanitize_text(value)
        text, omitted = _clip_text(safe, min(max_bytes, TRAJECTORY_DETAIL_FIELD_MAX_BYTES))
        return cls(text=text, omitted_bytes=omitted)

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
        if not isinstance(self.name, str) or not self.name:
            raise TrajectoryValidationError("detail field name must be a non-empty string")
        object.__setattr__(self, "name", sanitize_text(self.name))
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
    for field_value in fields:
        if not isinstance(field_value, DetailField):
            raise TrajectoryValidationError("record details must contain DetailField values")
        preview = field_value.preview
        if remaining <= 0:
            clipped = ContentPreview(
                text="", omitted_bytes=preview.encoded_bytes + preview.omitted_bytes
            )
        elif preview.encoded_bytes <= remaining:
            clipped = preview
        else:
            clipped = ContentPreview.from_text(
                preview.text,
                max_bytes=min(remaining, TRAJECTORY_DETAIL_FIELD_MAX_BYTES),
            )
            clipped = ContentPreview(
                text=clipped.text,
                omitted_bytes=clipped.omitted_bytes + preview.omitted_bytes,
            )
        bounded.append(DetailField(field_value.name, clipped, field_value.format))
        remaining -= clipped.encoded_bytes
    return tuple(bounded)


__all__ = [
    "ContentPreview",
    "DetailField",
    "bound_detail_fields",
    "escape_rich_text",
    "sanitize_text",
]
