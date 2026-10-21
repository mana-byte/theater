"""Filename and project-layout facts shared by Claude transcript entry points."""

from pathlib import Path


def is_jsonl_transcript(path: Path) -> bool:
    return path.suffix == ".jsonl"


def has_session_stem(path: Path, session_id: str) -> bool:
    return path.stem == session_id


def has_project_layout(relative: Path) -> bool:
    return len(relative.parts) == 2
