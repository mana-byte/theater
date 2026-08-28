"""Validation and loading for one declarative skill package."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from theater.constants.skills import (
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_FRONTMATTER_MAX_BYTES,
    SKILL_MAX_BYTES,
    SKILL_NAME,
    SKILL_NAME_MAX_CHARS,
)
from theater.skills.models import Skill, SkillSource, SkillValidationError

SKILL_FILENAME = "SKILL.md"


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _is_canonical_name(name: str) -> bool:
    """Whether ``name`` satisfies the skill identifier contract."""
    return len(name) <= SKILL_NAME_MAX_CHARS and SKILL_NAME.fullmatch(name) is not None


def _load_package(package_dir: Path, *, source: SkillSource, root: Path) -> Skill:
    """Load one package after proving it remains inside its discovery root."""
    _require_directory(package_dir, "skill package")
    _require_contained(package_dir, root)
    name = package_dir.name
    if not _is_canonical_name(name):
        raise SkillValidationError(
            f"directory name {name!r} must use lowercase ASCII letters, digits, and hyphens"
        )
    try:
        entries = tuple(package_dir.iterdir())
    except OSError as exc:
        raise SkillValidationError(
            f"cannot list skill package: {exc.strerror or 'I/O error'}"
        ) from exc
    if any(entry.is_symlink() for entry in entries):
        raise SkillValidationError("skill package must not contain symlinks")
    if len(entries) != 1 or entries[0].name != SKILL_FILENAME:
        raise SkillValidationError(f"skill package must contain only {SKILL_FILENAME}")
    content_path = entries[0]
    _require_regular_file(content_path, SKILL_FILENAME)
    _require_contained(content_path, root)
    content = _read_utf8(content_path)
    frontmatter, body = _frontmatter(content)
    metadata = _metadata(frontmatter)
    declared_name = metadata["name"]
    if declared_name != name:
        raise SkillValidationError(
            f"frontmatter name {declared_name!r} must match directory name {name!r}"
        )
    if not body.strip():
        raise SkillValidationError(
            f"{SKILL_FILENAME} must contain non-whitespace Markdown instructions "
            "after YAML frontmatter"
        )
    return Skill(
        name=name,
        description=metadata["description"],
        content=content,
        source=source,
        source_path=content_path,
    )


def _validate_root(root: Path) -> None:
    """Validate a discovery root before reading its child directories."""
    _require_directory(root, "skill root")


def _require_directory(path: Path, kind: str) -> None:
    if path.is_symlink():
        raise SkillValidationError(f"{kind} must not be a symlink")
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise SkillValidationError(f"cannot inspect {kind}: {exc.strerror or 'I/O error'}") from exc
    if not stat.S_ISDIR(mode):
        raise SkillValidationError(f"{kind} must be a directory")


def _require_regular_file(path: Path, kind: str) -> None:
    if path.is_symlink():
        raise SkillValidationError(f"{kind} must not be a symlink")
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise SkillValidationError(f"cannot inspect {kind}: {exc.strerror or 'I/O error'}") from exc
    if not stat.S_ISREG(mode):
        raise SkillValidationError(f"{kind} must be a regular file")


def _require_contained(path: Path, root: Path) -> None:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise SkillValidationError(f"cannot resolve path: {exc.strerror or 'I/O error'}") from exc
    if not resolved_path.is_relative_to(resolved_root):
        raise SkillValidationError("skill package path escapes its discovery root")


def _read_utf8(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(SKILL_MAX_BYTES + 1)
    except OSError as exc:
        raise SkillValidationError(
            f"cannot read {SKILL_FILENAME}: {exc.strerror or 'I/O error'}"
        ) from exc
    if len(data) > SKILL_MAX_BYTES:
        raise SkillValidationError(f"{SKILL_FILENAME} exceeds {SKILL_MAX_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillValidationError(f"{SKILL_FILENAME} must be valid UTF-8") from exc


def _frontmatter(content: str) -> tuple[str, str]:
    lines = content.splitlines(keepends=True)
    if not lines or not _delimiter(lines[0]):
        raise SkillValidationError(f"{SKILL_FILENAME} must start with YAML frontmatter")
    size = len(lines[0].encode("utf-8"))
    frontmatter: list[str] = []
    for index, line in enumerate(lines[1:], start=1):
        size += len(line.encode("utf-8"))
        if size > SKILL_FRONTMATTER_MAX_BYTES:
            raise SkillValidationError(f"frontmatter exceeds {SKILL_FRONTMATTER_MAX_BYTES} bytes")
        if _delimiter(line):
            return "".join(frontmatter), "".join(lines[index + 1 :])
        frontmatter.append(line)
    raise SkillValidationError("YAML frontmatter must end with ---")


def _delimiter(line: str) -> bool:
    return line.rstrip("\r\n") == "---"


def _metadata(frontmatter: str) -> dict[str, str]:
    try:
        parsed: Any = yaml.load(frontmatter, Loader=_StrictSafeLoader)
    except yaml.YAMLError as exc:
        raise SkillValidationError(
            f"invalid YAML frontmatter: {getattr(exc, 'problem', None) or 'parse error'}"
        ) from exc
    if not isinstance(parsed, dict) or set(parsed) != {"name", "description"}:
        raise SkillValidationError("frontmatter must contain exactly name and description")
    name = parsed["name"]
    description = parsed["description"]
    if not isinstance(name, str) or not _is_canonical_name(name):
        raise SkillValidationError("frontmatter name must be a canonical skill name")
    if not isinstance(description, str) or not description.strip():
        raise SkillValidationError("frontmatter description must be a non-whitespace string")
    if len(description) > SKILL_DESCRIPTION_MAX_CHARS:
        raise SkillValidationError(
            f"frontmatter description exceeds {SKILL_DESCRIPTION_MAX_CHARS} characters"
        )
    return {"name": name, "description": description}
