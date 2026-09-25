"""Portable JSON and readable Markdown exports of a loaded trajectory."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from regie.paths import paths_from_environment
from regie.trajectory.rich.inspection.content import Palette
from regie.trajectory.rich.inspection.sheet import build_sheet
from regie.trajectory.rich.render.formatting import format_duration
from regie.trajectory.ui_constants import (
    TRAJECTORY_EXPORT_EMPTY_MESSAGE,
    TRAJECTORY_EXPORT_PAYLOAD_MAX_CHARS,
    TRAJECTORY_EXPORT_SUBDIR,
    TRAJECTORY_EXPORT_TRUNCATION_MARKER,
)
from theater.frontend.trajectory import TrajectoryRecord, deterministic_record_order

_EXPORT_FORMAT = "theater.trajectory.export"
_EXPORT_VERSION = 1


@dataclass(frozen=True, slots=True)
class TrajectoryExport:
    json_path: Path
    markdown_path: Path


def serialize_trajectory_export(
    records: Sequence[TrajectoryRecord],
    *,
    participant_id: str,
    participant_name: str | None,
    participant_harness: str | None,
    exported_at: datetime,
    filter_query: str | None,
) -> tuple[str, str]:
    """Build stable machine-readable and human-readable export documents."""
    instant = exported_at.astimezone(UTC)
    ordered = tuple(deterministic_record_order(records))
    participant = {
        "id": participant_id,
        "name": participant_name,
        "harness": participant_harness,
    }
    document = {
        "format": _EXPORT_FORMAT,
        "version": _EXPORT_VERSION,
        "participant": participant,
        "exported_at": _timestamp(instant),
        "filter": filter_query,
        "records": [record.to_wire() for record in ordered],
    }
    json_text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    markdown = _markdown(ordered, participant, instant, filter_query)
    return json_text, markdown


def write_trajectory_export(
    records: Sequence[TrajectoryRecord],
    *,
    participant_id: str,
    participant_name: str | None,
    participant_harness: str | None,
    filter_query: str | None,
    exported_at: datetime | None = None,
) -> TrajectoryExport:
    """Write both formats atomically and return their final paths."""
    if not records:
        raise ValueError(TRAJECTORY_EXPORT_EMPTY_MESSAGE)
    instant = (exported_at or datetime.now(UTC)).astimezone(UTC)
    directory = trajectory_export_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    stem = f"{_safe_stem(participant_name or participant_id)}-{instant:%Y%m%dT%H%M%SZ}"
    json_text, markdown = serialize_trajectory_export(
        records,
        participant_id=participant_id,
        participant_name=participant_name,
        participant_harness=participant_harness,
        exported_at=instant,
        filter_query=filter_query,
    )
    json_path, markdown_path = _reserve_export_paths(directory, stem)
    try:
        _atomic_write(json_path, json_text)
        _atomic_write(markdown_path, markdown)
    except BaseException:
        json_path.unlink(missing_ok=True)
        markdown_path.unlink(missing_ok=True)
        raise
    return TrajectoryExport(json_path, markdown_path)


def trajectory_export_dir() -> Path:
    return paths_from_environment().root / TRAJECTORY_EXPORT_SUBDIR


def _markdown(
    records: Sequence[TrajectoryRecord],
    participant: dict[str, str | None],
    exported_at: datetime,
    filter_query: str | None,
) -> str:
    label = participant["name"] or participant["id"]
    lines = [
        f"# Trajectory export — {label}",
        "",
        f"- Participant: `{participant['id']}`",
        f"- Harness: {participant['harness'] or 'unknown'}",
        f"- Exported: {_timestamp(exported_at)}",
        f"- Filter: {filter_query if filter_query is not None else 'none'}",
        "",
    ]
    palette = Palette()
    for record in records:
        timing = record.timing
        start = (
            _timestamp(datetime.fromtimestamp(timing.start, UTC))
            if timing is not None and timing.start is not None
            else "—"
        )
        lines.extend(
            [
                f"### {record.lane.value} · {record.kind.value} · {record.status.value} · "
                f"{start} ({format_duration(timing)})",
                "",
            ]
        )
        if record.summary:
            lines.extend([record.summary, ""])
        for section in build_sheet(record, palette).sections:
            if section.key == "debug":
                continue
            payload = _truncate(section.copy_text)
            fence = _fence(payload)
            lines.extend([f"#### {section.title}", "", fence, payload, fence, ""])
    return "\n".join(lines).rstrip() + "\n"


def _truncate(value: str) -> str:
    limit = TRAJECTORY_EXPORT_PAYLOAD_MAX_CHARS
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}\n\n{TRAJECTORY_EXPORT_TRUNCATION_MARKER.format(count=omitted)}"


def _fence(value: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    return "`" * max(3, longest + 1)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return stem or "participant"


def _reserve_export_paths(directory: Path, stem: str) -> tuple[Path, Path]:
    suffix = 0
    while True:
        candidate = stem if suffix == 0 else f"{stem}-{suffix}"
        json_path = directory / f"{candidate}.json"
        markdown_path = directory / f"{candidate}.md"
        try:
            _reserve_file(json_path)
        except FileExistsError:
            suffix += 1
            continue
        try:
            _reserve_file(markdown_path)
        except FileExistsError:
            json_path.unlink(missing_ok=True)
            suffix += 1
            continue
        except BaseException:
            json_path.unlink(missing_ok=True)
            raise
        return json_path, markdown_path


def _reserve_file(path: Path) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)


def _atomic_write(path: Path, contents: str) -> None:
    descriptor, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "TrajectoryExport",
    "serialize_trajectory_export",
    "trajectory_export_dir",
    "write_trajectory_export",
]
