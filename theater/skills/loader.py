"""Validation and loading for one declarative skill package or play."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from theater.constants.skills import (
    PLAY_MANIFEST_FILENAME,
    PLAY_MANIFEST_MAX_BYTES,
    PLAY_SEPARATOR,
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_FRONTMATTER_MAX_BYTES,
    SKILL_MAX_BYTES,
    SKILL_MAX_COUNT,
    SKILL_NAME,
    SKILL_NAME_MAX_CHARS,
)
from theater.skills.models import Play, Skill, SkillSource, SkillValidationError
from theater.skills.plays import parse_play_manifest

SKILL_FILENAME = "SKILL.md"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_SKILL_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


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


def is_canonical_name(name: str) -> bool:
    """Whether ``name`` satisfies the skill identifier contract."""
    return len(name) <= SKILL_NAME_MAX_CHARS and SKILL_NAME.fullmatch(name) is not None


def is_skill_reference(name: str) -> bool:
    """A standalone skill name, or a play member qualified as ``<play>/<member>``."""
    parts = name.split(PLAY_SEPARATOR)
    return len(parts) <= 2 and all(is_canonical_name(part) for part in parts)


def skill_body(content: str) -> str:
    """The Markdown instructions of a validated ``SKILL.md``, without its frontmatter."""
    return _frontmatter(content)[1]


@dataclass(frozen=True, slots=True)
class LoadedPlay:
    """One validated play and its member skills, named ``<play>/<member>``."""

    play: Play
    skills: tuple[Skill, ...]


def load_entry(name: str, *, source: SkillSource, root: Path, root_fd: int) -> Skill | LoadedPlay:
    """A directory holding PLAY.toml is a play; any other is one standalone skill package."""
    if is_canonical_name(name) and _holds_manifest(name, root_fd):
        return _load_play(name, source=source, root=root, root_fd=root_fd)
    return _load_package(name, source=source, root=root, root_fd=root_fd)


def _holds_manifest(name: str, root_fd: int) -> bool:
    try:
        package_fd = _open_directory(name, "skill package", dir_fd=root_fd)
    except SkillValidationError:
        return False
    assert package_fd is not None
    try:
        os.stat(PLAY_MANIFEST_FILENAME, dir_fd=package_fd, follow_symlinks=False)
    except OSError:
        return False
    finally:
        _close_fd(package_fd)
    return True


def _load_play(name: str, *, source: SkillSource, root: Path, root_fd: int) -> LoadedPlay:
    play_fd = _open_directory(name, "play package", dir_fd=root_fd)
    assert play_fd is not None
    try:
        try:
            entries, overflow = _bounded_entry_names(play_fd, limit=SKILL_MAX_COUNT + 1)
        except OSError as exc:
            raise SkillValidationError(
                f"cannot list play package: {exc.strerror or 'I/O error'}"
            ) from exc
        if overflow:
            raise SkillValidationError(f"play package exceeds {SKILL_MAX_COUNT} members")
        manifest_fd = _open_regular_file(PLAY_MANIFEST_FILENAME, play_fd)
        try:
            text = _read_utf8(
                manifest_fd, limit=PLAY_MANIFEST_MAX_BYTES, filename=PLAY_MANIFEST_FILENAME
            )
        finally:
            _close_fd(manifest_fd)
        description, rules = parse_play_manifest(text, play=name)
        members = set(entries) - {PLAY_MANIFEST_FILENAME}
        declared = set(rules.positions) | set(rules.roles)
        if members != declared:
            missing = ", ".join(sorted(declared - members)) or "none"
            extra = ", ".join(sorted(members - declared)) or "none"
            raise SkillValidationError(
                f"a play holds {PLAY_MANIFEST_FILENAME} and one member package per declared "
                f"position and role; missing: {missing}; undeclared: {extra}"
            )
        skills: list[Skill] = []
        for member in sorted(members):
            try:
                skill = _load_package(member, source=source, root=root / name, root_fd=play_fd)
            except SkillValidationError as exc:
                raise SkillValidationError(f"member {member!r}: {exc}") from exc
            skills.append(replace(skill, name=rules.qualified(member), play=name))
    finally:
        _close_fd(play_fd)
    play = Play(
        name=name,
        description=description,
        rules=rules,
        source=source,
        source_path=root / name / PLAY_MANIFEST_FILENAME,
    )
    return LoadedPlay(play=play, skills=tuple(skills))


def _load_package(
    package_name: str,
    *,
    source: SkillSource,
    root: Path,
    root_fd: int,
    provider: str | None = None,
) -> Skill:
    """Load one package through the already-open discovery root."""
    name = package_name
    if not is_canonical_name(name):
        raise SkillValidationError(
            f"directory name {name!r} must use lowercase ASCII letters, digits, and hyphens"
        )
    package_fd = _open_directory(name, "skill package", dir_fd=root_fd)
    assert package_fd is not None
    try:
        try:
            entries, overflow = _bounded_entry_names(package_fd, limit=1)
        except OSError as exc:
            raise SkillValidationError(
                f"cannot list skill package: {exc.strerror or 'I/O error'}"
            ) from exc
        if overflow or entries != (SKILL_FILENAME,):
            raise SkillValidationError(f"skill package must contain only {SKILL_FILENAME}")
        skill_fd = _open_skill_file(package_fd)
        try:
            content = _read_utf8(skill_fd)
        finally:
            _close_fd(skill_fd)
    finally:
        _close_fd(package_fd)
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
        source_path=root / name / SKILL_FILENAME,
        provider=provider,
    )


def load_declared_skills(
    root: Path,
    names: Sequence[str],
    *,
    provider: str,
) -> tuple[Skill, ...]:
    """Load explicitly declared MCP-plugin skills from one package-owned root."""
    if not names:
        return ()
    root_fd = _open_directory(root, "MCP-plugin skill root")
    assert root_fd is not None
    try:
        loaded: list[Skill] = []
        for name in names:
            try:
                loaded.append(
                    _load_package(
                        name,
                        source=SkillSource.MCP_PLUGIN,
                        root=root,
                        root_fd=root_fd,
                        provider=provider,
                    )
                )
            except SkillValidationError as exc:
                raise SkillValidationError(f"registered skill {name!r}: {exc}") from exc
        return tuple(loaded)
    finally:
        _close_fd(root_fd)


def _open_directory(
    path: Path | str, kind: str, *, dir_fd: int | None = None, missing_ok: bool = False
) -> int | None:
    try:
        fd = os.open(path, _DIRECTORY_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError as exc:
        if missing_ok:
            return None
        raise SkillValidationError(f"cannot inspect {kind}: {exc.strerror or 'I/O error'}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP or _is_symlink(path, dir_fd=dir_fd):
            raise SkillValidationError(f"{kind} must not be a symlink") from exc
        if exc.errno == errno.ENOTDIR:
            raise SkillValidationError(f"{kind} must be a directory") from exc
        raise SkillValidationError(f"cannot inspect {kind}: {exc.strerror or 'I/O error'}") from exc
    try:
        mode = os.fstat(fd).st_mode
    except OSError as exc:
        _close_fd(fd)
        raise SkillValidationError(f"cannot inspect {kind}: {exc.strerror or 'I/O error'}") from exc
    if not stat.S_ISDIR(mode):
        _close_fd(fd)
        raise SkillValidationError(f"{kind} must be a directory")
    return fd


def _bounded_entry_names(directory_fd: int, *, limit: int) -> tuple[tuple[str, ...], bool]:
    scan_fd = os.dup(directory_fd)
    try:
        entries = _open_scandir(scan_fd)
        names: list[str] = []
        with entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > limit:
                    return tuple(names), True
        return tuple(names), False
    finally:
        _close_fd(scan_fd)


def _open_scandir(fd: int):
    return os.scandir(fd)


def _open_skill_file(package_fd: int) -> int:
    return _open_regular_file(SKILL_FILENAME, package_fd)


def _open_regular_file(filename: str, directory_fd: int) -> int:
    try:
        fd = os.open(filename, _SKILL_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP or _is_symlink(filename, dir_fd=directory_fd):
            raise SkillValidationError(f"{filename} must not be a symlink") from exc
        raise SkillValidationError(
            f"cannot inspect {filename}: {exc.strerror or 'I/O error'}"
        ) from exc
    try:
        mode = os.fstat(fd).st_mode
    except OSError as exc:
        _close_fd(fd)
        raise SkillValidationError(
            f"cannot inspect {filename}: {exc.strerror or 'I/O error'}"
        ) from exc
    if not stat.S_ISREG(mode):
        _close_fd(fd)
        raise SkillValidationError(f"{filename} must be a regular file")
    return fd


def _close_fd(fd: int) -> None:
    with suppress(OSError):
        os.close(fd)


def _is_symlink(path: Path | str, *, dir_fd: int | None) -> bool:
    try:
        return stat.S_ISLNK(os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _read_utf8(fd: int, *, limit: int = SKILL_MAX_BYTES, filename: str = SKILL_FILENAME) -> str:
    data = bytearray()
    try:
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
    except OSError as exc:
        raise SkillValidationError(
            f"cannot read {filename}: {exc.strerror or 'I/O error'}"
        ) from exc
    if len(data) > limit:
        raise SkillValidationError(f"{filename} exceeds {limit} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillValidationError(f"{filename} must be valid UTF-8") from exc


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
    if not isinstance(name, str) or not is_canonical_name(name):
        raise SkillValidationError("frontmatter name must be a canonical skill name")
    if not isinstance(description, str) or not description.strip():
        raise SkillValidationError("frontmatter description must be a non-whitespace string")
    if not description.isprintable():
        raise SkillValidationError("frontmatter description must contain only printable characters")
    if len(description) > SKILL_DESCRIPTION_MAX_CHARS:
        raise SkillValidationError(
            f"frontmatter description exceeds {SKILL_DESCRIPTION_MAX_CHARS} characters"
        )
    return {"name": name, "description": description}
