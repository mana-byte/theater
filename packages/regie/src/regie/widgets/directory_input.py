"""Directory-only path completion for Régie launch dialogs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.suggester import Suggester
from textual.widgets import Input


def normalize_directory(value: str, *, base_dir: Path) -> str:
    """Return an absolute existing directory without resolving symlinks."""
    if not value:
        raise ValueError("working directory is required")
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = base_dir / path
        path = path.absolute()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"cannot use working directory: {value}") from exc
    if not path.is_dir():
        raise ValueError(f"not an existing directory: {value}")
    return str(path)


def directory_suggestion(value: str, *, base_dir: Path) -> str | None:
    """Complete the unambiguous directory portion of a user-entered path."""
    if not value:
        return None
    try:
        exact = _absolute_path(value, base_dir)
        if not value.endswith(os.sep) and exact.is_dir():
            return f"{value}{os.sep}"

        if value.endswith(os.sep):
            display_prefix, parent_text, fragment = value, value, ""
        else:
            head, separator, fragment = value.rpartition(os.sep)
            display_prefix = f"{head}{separator}"
            parent_text = head if head else (os.sep if separator else ".")
        parent = _absolute_path(parent_text, base_dir)
        matches = sorted(
            (
                child.name
                for child in parent.iterdir()
                if child.is_dir()
                and child.name.startswith(fragment)
                and (not child.name.startswith(".") or fragment.startswith("."))
            ),
            key=str.casefold,
        )
    except (OSError, RuntimeError, ValueError):
        return None
    if not matches:
        return None
    if len(matches) == 1:
        return f"{display_prefix}{matches[0]}{os.sep}"
    common = os.path.commonprefix(matches)
    if len(common) > len(fragment):
        return f"{display_prefix}{common}"
    return None


def _absolute_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_dir / path


class DirectorySuggester(Suggester):
    """Feed live filesystem completions to Textual's native ghost text."""

    def __init__(self, base_dir: Path) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self.base_dir = base_dir

    async def get_suggestion(self, value: str) -> str | None:
        return directory_suggestion(value, base_dir=self.base_dir)


class DirectoryInput(Input):
    """An input where Tab and Right accept directory completion."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("tab", "complete_directory", "Complete directory", show=False, priority=True)
    ]

    def __init__(
        self,
        *,
        value: str,
        base_dir: Path,
        placeholder: str = "working directory",
        id: str | None = None,
    ) -> None:
        self._directory_suggester = DirectorySuggester(base_dir)
        super().__init__(
            value=value,
            placeholder=placeholder,
            suggester=self._directory_suggester,
            id=id,
        )

    def action_complete_directory(self) -> None:
        suggestion = directory_suggestion(
            self.value,
            base_dir=self._directory_suggester.base_dir,
        )
        if suggestion is not None:
            self.value = suggestion
            self.cursor_position = len(suggestion)


__all__ = [
    "DirectoryInput",
    "DirectorySuggester",
    "directory_suggestion",
    "normalize_directory",
]
