"""Focused tests for declarative skill loading and registry discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from theater import paths
from theater.constants.skills import (
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_FRONTMATTER_MAX_BYTES,
    SKILL_MAX_BYTES,
    SKILL_MAX_COUNT,
)
from theater.skills import (
    BuiltinSkillError,
    Skill,
    SkillRegistry,
    SkillSource,
    UnknownSkill,
    discover,
)


def write_skill(root: Path, name: str, *, description: str = "A useful skill.") -> Path:
    package = root / name
    package.mkdir(parents=True)
    content = f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n"
    (package / "SKILL.md").write_text(content, encoding="utf-8")
    return package


def write_raw(root: Path, name: str, content: str | bytes) -> Path:
    package = root / name
    package.mkdir(parents=True)
    path = package / "SKILL.md"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return package


def test_shipped_skills_are_discovered_with_their_exact_content(tmp_path):
    registry = discover(user_dir=tmp_path)

    assert [skill.name for skill in registry.skills] == ["theater-debate", "theater-orchestrate"]
    skill = registry.load("theater-orchestrate")
    assert skill.source is SkillSource.BUILTIN
    assert skill.content == skill.source_path.read_text(encoding="utf-8")
    assert skill.description.startswith("Orchestrate implementation")


def test_valid_user_skill_is_loadable_in_deterministic_name_order(tmp_path):
    write_skill(tmp_path, "zebra")
    write_skill(tmp_path, "alpha")

    registry = discover(user_dir=tmp_path)

    assert [skill.name for skill in registry.skills] == [
        "alpha",
        "theater-debate",
        "theater-orchestrate",
        "zebra",
    ]
    skill = registry.load("alpha")
    assert skill.source is SkillSource.USER
    assert skill.content == "---\nname: alpha\ndescription: A useful skill.\n---\n\n# alpha\n"
    with pytest.raises(UnknownSkill, match="canonical"):
        registry.load(str(tmp_path / "alpha"))


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("missing", "# Missing frontmatter\n", "must start with YAML frontmatter"),
        ("extra", "---\nname: extra\ndescription: x\nextra: x\n---\n", "exactly"),
        ("only-name", "---\nname: only-name\n---\n", "exactly"),
        ("invalid-yaml", "---\nname: [\ndescription: x\n---\n", "invalid YAML"),
        ("duplicate", "---\nname: duplicate\nname: duplicate\ndescription: x\n---\n", "duplicate"),
    ],
)
def test_malformed_missing_and_extra_frontmatter_are_rejected(tmp_path, name, content, expected):
    write_raw(tmp_path, name, content)

    registry = discover(user_dir=tmp_path)

    assert registry.rejections[0].name == name
    assert expected in registry.rejections[0].error


def test_name_path_mismatch_is_rejected(tmp_path):
    write_raw(tmp_path, "directory-name", "---\nname: another-name\ndescription: x\n---\n")

    registry = discover(user_dir=tmp_path)

    assert "must match directory name" in registry.rejections[0].error


def test_whitespace_only_description_is_rejected_without_normalizing_authored_text(tmp_path):
    write_raw(
        tmp_path,
        "blank-description",
        "---\nname: blank-description\ndescription: '   '\n---\n\n# Instructions\n",
    )
    write_raw(
        tmp_path,
        "preserved-description",
        "---\nname: preserved-description\ndescription: '  Keep this spacing.  '\n"
        "---\n\n# Instructions\n",
    )

    registry = discover(user_dir=tmp_path)

    assert "non-whitespace" in registry.rejections[0].error
    assert registry.load("preserved-description").description == "  Keep this spacing.  "


def test_whitespace_only_markdown_body_is_rejected(tmp_path):
    write_raw(tmp_path, "blank-body", "---\nname: blank-body\ndescription: x\n---\n \t\n")

    registry = discover(user_dir=tmp_path)

    assert "Markdown instructions after YAML frontmatter" in registry.rejections[0].error


def test_invalid_utf8_and_oversized_skill_files_are_rejected(tmp_path):
    write_raw(tmp_path, "bad-utf8", b"---\nname: bad-utf8\ndescription: \xff\n---\n")
    write_raw(
        tmp_path,
        "too-large",
        "---\nname: too-large\ndescription: x\n---\n" + "x" * SKILL_MAX_BYTES,
    )

    registry = discover(user_dir=tmp_path)

    errors = {rejection.name: rejection.error for rejection in registry.rejections}
    assert "valid UTF-8" in errors["bad-utf8"]
    assert "exceeds" in errors["too-large"]


def test_frontmatter_and_description_size_limits_are_rejected(tmp_path):
    write_raw(
        tmp_path,
        "long-description",
        "---\nname: long-description\ndescription: "
        + "x" * (SKILL_DESCRIPTION_MAX_CHARS + 1)
        + "\n---\n",
    )
    write_raw(
        tmp_path,
        "long-frontmatter",
        "---\nname: long-frontmatter\ndescription: "
        + "x" * SKILL_FRONTMATTER_MAX_BYTES
        + "\n---\n",
    )

    registry = discover(user_dir=tmp_path)

    errors = {rejection.name: rejection.error for rejection in registry.rejections}
    assert "description exceeds" in errors["long-description"]
    assert "frontmatter exceeds" in errors["long-frontmatter"]


def test_symlink_and_path_escape_candidates_are_rejected(tmp_path):
    outside = tmp_path.parent / "outside-skill"
    write_skill(outside, "escape")
    (tmp_path / "escape").symlink_to(outside / "escape", target_is_directory=True)
    package = write_skill(tmp_path, "linked-file")
    (package / "SKILL.md").unlink()
    (package / "SKILL.md").symlink_to(outside / "escape" / "SKILL.md")

    registry = discover(user_dir=tmp_path)

    assert {rejection.name for rejection in registry.rejections} == {"escape", "linked-file"}
    assert all("symlink" in rejection.error for rejection in registry.rejections)


def test_extra_unsupported_package_contents_are_rejected(tmp_path):
    package = write_skill(tmp_path, "extra-file")
    (package / "README.md").write_text("not allowed", encoding="utf-8")

    registry = discover(user_dir=tmp_path)

    assert registry.rejections[0].name == "extra-file"
    assert registry.rejections[0].error == "skill package must contain only SKILL.md"


def test_user_builtin_name_collision_cannot_override_shipped_skill(tmp_path):
    package = write_skill(tmp_path, "theater-debate", description="A conflicting user definition.")

    registry = discover(user_dir=tmp_path)

    assert registry.load("theater-debate").source is SkillSource.BUILTIN
    (rejection,) = registry.rejections
    assert rejection.name == "theater-debate"
    assert str(package / "SKILL.md") in rejection.error
    assert "builtin/theater-debate/SKILL.md" in rejection.error


def test_invalid_user_skill_is_diagnostic_but_invalid_builtin_is_fatal(tmp_path):
    write_raw(tmp_path, "broken-user", "---\nname: broken-user\n---\n")

    registry = discover(user_dir=tmp_path)

    assert registry.load("theater-debate").source is SkillSource.BUILTIN
    assert registry.rejections[0].name == "broken-user"

    builtin = tmp_path / "builtin"
    write_raw(builtin, "broken-builtin", "---\nname: broken-builtin\n---\n")
    with pytest.raises(BuiltinSkillError, match="invalid bundled skill"):
        discover(builtin_dir=builtin, user_dir=tmp_path / "empty")


def test_empty_user_root_is_allowed(tmp_path):
    registry = discover(user_dir=tmp_path / "missing")

    assert [skill.name for skill in registry.skills] == ["theater-debate", "theater-orchestrate"]
    assert registry.rejections == ()


def test_discovery_returns_a_fresh_bounded_snapshot(tmp_path):
    for number in range(SKILL_MAX_COUNT + 1):
        write_skill(tmp_path, f"skill-{number}")

    registry = discover(user_dir=tmp_path)

    assert (
        len([skill for skill in registry.skills if skill.source is SkillSource.USER])
        == SKILL_MAX_COUNT
    )
    assert any("exceeds the limit" in rejection.error for rejection in registry.rejections)
    write_skill(tmp_path, "later")
    refreshed = discover(user_dir=tmp_path)
    assert all(skill.name != "later" for skill in registry.skills)
    assert refreshed.load("later").source is SkillSource.USER


def test_direct_registry_construction_sorts_skills_by_name():
    alpha = Skill("alpha", "a", "alpha", SkillSource.USER, Path("/tmp/alpha/SKILL.md"))
    zebra = Skill("zebra", "z", "zebra", SkillSource.USER, Path("/tmp/zebra/SKILL.md"))

    registry = SkillRegistry({"zebra": zebra, "alpha": alpha}, ())

    assert [skill.name for skill in registry.skills] == ["alpha", "zebra"]


def test_ensure_home_creates_the_skills_directory(monkeypatch, tmp_path):
    root = tmp_path / "state"
    monkeypatch.setenv("THEATER_HOME", str(root))

    paths.ensure_home()

    assert paths.skills_dir() == root / "skills"
    assert paths.skills_dir().is_dir()
