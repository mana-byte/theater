"""Fresh, immutable multi-root snapshots of declarative skill packages and plays."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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
    LoadedPlay,
    _bounded_entry_names,
    _close_fd,
    _open_directory,
    is_skill_reference,
    load_entry,
)
from theater.skills.models import (
    BuiltinSkillError,
    Play,
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
    _plays: Mapping[str, Play] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_skills",
            MappingProxyType({name: self._skills[name] for name in sorted(self._skills)}),
        )
        object.__setattr__(self, "rejections", tuple(self.rejections))
        object.__setattr__(
            self,
            "_plays",
            MappingProxyType({name: self._plays[name] for name in sorted(self._plays)}),
        )

    @property
    def skills(self) -> tuple[Skill, ...]:
        """Every loadable skill, play members included, in deterministic name order."""
        return tuple(self._skills.values())

    @property
    def plays(self) -> tuple[Play, ...]:
        return tuple(self._plays.values())

    def play(self, name: str) -> Play | None:
        return self._plays.get(name)

    def load(self, name: str) -> Skill:
        """Load a skill by its canonical name, or a play member by ``<play>/<member>``."""
        if not isinstance(name, str) or not is_skill_reference(name):
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

    ``disabled`` applies only to built-ins (a play by its name), all validated before filtering.
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
            "Reinstall Theater or repair its packaged SKILL.md or PLAY.toml."
        )
    disabled_names = set(disabled)
    skills: dict[str, Skill] = {}
    plays: dict[str, Play] = {}
    for item in builtins:
        if isinstance(item, SkillRejection) or _entry_name(item) in disabled_names:
            continue
        _add(skills, plays, item)
    rejections: list[SkillRejection] = []
    for item in _scan_root(user_root, SkillSource.USER, required=False):
        if isinstance(item, SkillRejection):
            _append_rejection(rejections, item)
            continue
        name = _entry_name(item)
        builtin = plays.get(name) or skills.get(name)
        if builtin is not None:
            path = item.play.source_path if isinstance(item, LoadedPlay) else item.source_path
            _append_rejection(
                rejections,
                _rejection(
                    SkillSource.USER,
                    path,
                    name,
                    f"user skill at {path} conflicts with bundled skill at "
                    f"{builtin.source_path}; bundled skill remains authoritative",
                ),
            )
            continue
        _add(skills, plays, item)
    _merge_plugin_skills(skills, rejections, plugin_skills, plays=plays)
    return SkillRegistry(skills, tuple(rejections), plays)


def _entry_name(item: Skill | LoadedPlay) -> str:
    return item.play.name if isinstance(item, LoadedPlay) else item.name


def _add(skills: dict[str, Skill], plays: dict[str, Play], item: Skill | LoadedPlay) -> None:
    if isinstance(item, LoadedPlay):
        plays[item.play.name] = item.play
        skills.update((skill.name, skill) for skill in item.skills)
    else:
        skills[item.name] = item


def _merge_plugin_skills(
    skills: dict[str, Skill],
    rejections: list[SkillRejection],
    plugin_skills: Iterable[Skill],
    *,
    plays: Mapping[str, Play] = MappingProxyType({}),
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
        existing: Skill | Play | None = skills.get(name) or plays.get(name)
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
) -> tuple[Skill | LoadedPlay | SkillRejection, ...]:
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
        results: list[Skill | LoadedPlay | SkillRejection] = []
        for name in sorted(entries):
            try:
                results.append(load_entry(name, source=source, root=root, root_fd=root_fd))
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
