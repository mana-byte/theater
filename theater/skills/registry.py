"""Fresh, immutable multi-root snapshots of declarative skill packages."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
from theater.skills.loader import (
    _bounded_entry_names,
    _close_fd,
    _load_package,
    _open_directory,
    is_canonical_name,
)
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
        if not isinstance(name, str) or not is_canonical_name(name):
            raise UnknownSkill("skill name must be canonical")
        try:
            return self._skills[name]
        except KeyError as exc:
            raise UnknownSkill(f"unknown skill {name!r}") from exc


def discover(
    *,
    builtin_dir: Path | None = None,
    user_dir: Path | None = None,
    plugin_skills: Iterable[Skill] = (),
    disabled: Iterable[str] = (),
) -> SkillRegistry:
    """Build a fresh snapshot.

    ``disabled`` applies only to built-ins, all of which are validated before filtering.
    """
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
    disabled_names = set(disabled)
    skills = {
        item.name: item
        for item in builtins
        if isinstance(item, Skill) and item.name not in disabled_names
    }
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
    _merge_plugin_skills(skills, rejections, plugin_skills)
    ordered = {name: skills[name] for name in sorted(skills)}
    return SkillRegistry(ordered, tuple(rejections))


def _merge_plugin_skills(
    skills: dict[str, Skill],
    rejections: list[SkillRejection],
    plugin_skills: Iterable[Skill],
) -> None:
    groups: dict[str, list[Skill]] = {}
    for skill in plugin_skills:
        if not isinstance(skill, Skill) or skill.source is not SkillSource.MCP_PLUGIN:
            raise TypeError("plugin_skills must contain MCP-plugin Skill values")
        groups.setdefault(skill.name, []).append(skill)
    for name in sorted(groups):
        candidates = groups[name]
        if len(candidates) > 1:
            providers = ", ".join(sorted({item.provider or "unknown" for item in candidates}))
            for candidate in candidates:
                _append_rejection(
                    rejections,
                    _rejection(
                        candidate.source,
                        candidate.source_path,
                        candidate.name,
                        f"skill name {name!r} is registered by multiple enabled MCP plugins "
                        f"({providers}); no plugin registration is active",
                        provider=candidate.provider,
                    ),
                )
            continue
        candidate = candidates[0]
        existing = skills.get(name)
        if existing is not None:
            _append_rejection(
                rejections,
                _rejection(
                    candidate.source,
                    candidate.source_path,
                    candidate.name,
                    f"MCP plugin {candidate.provider!r} skill at {candidate.source_path} "
                    f"conflicts with {existing.source.value} skill at {existing.source_path}; "
                    "the existing skill remains authoritative",
                    provider=candidate.provider,
                ),
            )
            continue
        skills[name] = candidate


def _scan_root(
    root: Path, source: SkillSource, *, required: bool
) -> tuple[Skill | SkillRejection, ...]:
    try:
        root_fd = _open_directory(root, "skill root", missing_ok=not required)
    except SkillValidationError as exc:
        return (_rejection(source, root, None, str(exc)),)
    if root_fd is None:
        return ()
    try:
        try:
            entries, overflow = _bounded_entry_names(root_fd, limit=SKILL_MAX_COUNT)
        except OSError as exc:
            return (
                _rejection(
                    source, root, None, f"cannot list skill root: {exc.strerror or 'I/O error'}"
                ),
            )
        if overflow:
            return (
                _rejection(
                    source,
                    root,
                    None,
                    f"skill root exceeds the limit of {SKILL_MAX_COUNT} entries",
                ),
            )
        results: list[Skill | SkillRejection] = []
        for name in sorted(entries):
            try:
                results.append(_load_package(name, source=source, root=root, root_fd=root_fd))
            except SkillValidationError as exc:
                results.append(_rejection(source, root / name, name, str(exc)))
        return tuple(results)
    finally:
        _close_fd(root_fd)


def _append_rejection(rejections: list[SkillRejection], rejection: SkillRejection) -> None:
    if len(rejections) < SKILL_MAX_REJECTIONS:
        rejections.append(rejection)


def _rejection(
    source: SkillSource,
    path: Path,
    name: str | None,
    error: str,
    *,
    provider: str | None = None,
) -> SkillRejection:
    return SkillRejection(source, path, name, _bounded(error), provider)


def _bounded(value: str) -> str:
    normalized = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    if len(normalized) <= SKILL_DIAGNOSTIC_MAX_CHARS:
        return normalized
    marker = "… (truncated)"
    return f"{normalized[: SKILL_DIAGNOSTIC_MAX_CHARS - len(marker)]}{marker}"
