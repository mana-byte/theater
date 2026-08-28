"""Fresh, immutable multi-root snapshots of declarative skill packages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType

from theater import paths
from theater.constants.skills import (
    SKILL_DIAGNOSTIC_MAX_CHARS,
    SKILL_MAX_COUNT,
    SKILL_MAX_REJECTIONS,
)
from theater.skills.loader import _is_canonical_name, _load_package, _validate_root
from theater.skills.models import (
    BuiltinSkillError,
    Skill,
    SkillRejection,
    SkillSource,
    SkillValidationError,
    UnknownSkill,
)


@dataclass(frozen=True, slots=True)
class SkillRegistry:
    """A deterministic, immutable discovery result."""

    _skills: Mapping[str, Skill]
    rejections: tuple[SkillRejection, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_skills",
            MappingProxyType({name: self._skills[name] for name in sorted(self._skills)}),
        )
        object.__setattr__(self, "rejections", tuple(self.rejections))

    @property
    def skills(self) -> tuple[Skill, ...]:
        """Validated skills in deterministic name order."""
        return tuple(self._skills.values())

    def load(self, name: str) -> Skill:
        """Load a skill by its canonical name from this snapshot."""
        if not isinstance(name, str) or not _is_canonical_name(name):
            raise UnknownSkill("skill name must be canonical")
        try:
            return self._skills[name]
        except KeyError as exc:
            raise UnknownSkill(f"unknown skill {name!r}") from exc


def discover(*, builtin_dir: Path | None = None, user_dir: Path | None = None) -> SkillRegistry:
    """Build a fresh bounded snapshot with built-ins authoritative over user skills."""
    user_root = user_dir if user_dir is not None else paths.skills_dir()
    if builtin_dir is None:
        resource = resources.files("theater.skills").joinpath("builtin")
        with resources.as_file(resource) as builtin_root:
            builtins = _scan_root(builtin_root, SkillSource.BUILTIN, required=True)
    else:
        builtins = _scan_root(builtin_dir, SkillSource.BUILTIN, required=True)
    builtin_rejection = next((item for item in builtins if isinstance(item, SkillRejection)), None)
    if builtin_rejection is not None:
        raise BuiltinSkillError(
            f"invalid bundled skill at {builtin_rejection.source_path}: {builtin_rejection.error}. "
            "Reinstall Theater or repair its packaged SKILL.md."
        )
    skills = {item.name: item for item in builtins if isinstance(item, Skill)}
    rejections: list[SkillRejection] = []
    for item in _scan_root(user_root, SkillSource.USER, required=False):
        if isinstance(item, SkillRejection):
            _append_rejection(rejections, item)
            continue
        builtin = skills.get(item.name)
        if builtin is not None:
            _append_rejection(
                rejections,
                _rejection(
                    item.source,
                    item.source_path,
                    item.name,
                    f"user skill at {item.source_path} conflicts with bundled skill at "
                    f"{builtin.source_path}; bundled skill remains authoritative",
                ),
            )
            continue
        skills[item.name] = item
    ordered = {name: skills[name] for name in sorted(skills)}
    return SkillRegistry(ordered, tuple(rejections))


def _scan_root(
    root: Path, source: SkillSource, *, required: bool
) -> tuple[Skill | SkillRejection, ...]:
    try:
        absent = not root.exists() and not root.is_symlink()
    except OSError as exc:
        return (
            _rejection(
                source, root, None, f"cannot inspect skill root: {exc.strerror or 'I/O error'}"
            ),
        )
    if absent and not required:
        return ()
    try:
        _validate_root(root)
    except SkillValidationError as exc:
        return (_rejection(source, root, None, str(exc)),)
    try:
        entries = sorted(root.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        return (
            _rejection(
                source, root, None, f"cannot list skill root: {exc.strerror or 'I/O error'}"
            ),
        )
    results: list[Skill | SkillRejection] = []
    for entry in entries[:SKILL_MAX_COUNT]:
        try:
            results.append(_load_package(entry, source=source, root=root))
        except SkillValidationError as exc:
            results.append(_rejection(source, entry, entry.name, str(exc)))
    if len(entries) > SKILL_MAX_COUNT:
        results.append(
            _rejection(
                source,
                root,
                None,
                f"skill root exceeds the limit of {SKILL_MAX_COUNT} entries",
            )
        )
    return tuple(results)


def _append_rejection(rejections: list[SkillRejection], rejection: SkillRejection) -> None:
    if len(rejections) < SKILL_MAX_REJECTIONS:
        rejections.append(rejection)


def _rejection(source: SkillSource, path: Path, name: str | None, error: str) -> SkillRejection:
    return SkillRejection(source, path, name, _bounded(error))


def _bounded(value: str) -> str:
    normalized = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    if len(normalized) <= SKILL_DIAGNOSTIC_MAX_CHARS:
        return normalized
    marker = "… (truncated)"
    return f"{normalized[: SKILL_DIAGNOSTIC_MAX_CHARS - len(marker)]}{marker}"
