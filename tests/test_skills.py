"""Focused tests for declarative skill loading and registry discovery."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from theater import paths
from theater.constants.skills import (
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_FRONTMATTER_MAX_BYTES,
    SKILL_MAX_BYTES,
    SKILL_MAX_COUNT,
)
from theater.daemon.rpc import skills as skills_rpc
from theater.models import BadRequest, NotFound
from theater.skills import (
    BuiltinSkillError,
    Skill,
    SkillRegistry,
    SkillRejection,
    SkillSource,
    UnknownSkill,
    discover,
    is_canonical_name,
)
from theater.skills import loader as skills_loader


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

    assert [skill.name for skill in registry.skills] == [
        "theater-configure",
        "theater-debate",
        "theater-orchestrate",
        "theater-recover-tmux",
    ]
    skill = registry.load("theater-orchestrate")
    assert skill.source is SkillSource.BUILTIN
    assert skill.content == skill.source_path.read_text(encoding="utf-8")


def test_shipped_tmux_recovery_skill_has_exact_data_only_package(tmp_path):
    registry = discover(user_dir=tmp_path)

    skill = registry.load("theater-recover-tmux")

    assert skill.source is SkillSource.BUILTIN
    assert skill.name == "theater-recover-tmux"
    assert skill.content == skill.source_path.read_text(encoding="utf-8")
    assert sorted(entry.name for entry in skill.source_path.parent.iterdir()) == ["SKILL.md"]


def test_valid_user_skill_is_loadable_in_deterministic_name_order(tmp_path):
    write_skill(tmp_path, "zebra")
    write_skill(tmp_path, "alpha")

    registry = discover(user_dir=tmp_path)

    assert [skill.name for skill in registry.skills] == [
        "alpha",
        "theater-configure",
        "theater-debate",
        "theater-orchestrate",
        "theater-recover-tmux",
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


@pytest.mark.parametrize(
    ("name", "escaped"),
    [
        ("tab-description", r"has\ttab"),
        ("newline-description", r"has\nnewline"),
        ("escape-description", r"has\eescape"),
    ],
)
def test_control_characters_in_descriptions_are_rejected(tmp_path, name, escaped):
    write_raw(
        tmp_path,
        name,
        f'---\nname: {name}\ndescription: "{escaped}"\n---\n\n# Instructions\n',
    )

    registry = discover(user_dir=tmp_path)

    assert registry.rejections[0].name == name
    assert "printable" in registry.rejections[0].error


def test_printable_unicode_description_is_preserved(tmp_path):
    write_raw(
        tmp_path,
        "unicode-description",
        '---\nname: unicode-description\ndescription: "  Café — useful  "\n---\n\n# Instructions\n',
    )

    registry = discover(user_dir=tmp_path)

    assert registry.load("unicode-description").description == "  Café — useful  "


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


def test_skill_file_swap_to_symlink_is_rejected_from_the_open_descriptor(tmp_path, monkeypatch):
    package = write_skill(tmp_path, "swapped")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("outside", encoding="utf-8")
    original = skills_loader._bounded_entry_names

    def swap_after_listing(fd, *, limit):
        entries, overflow = original(fd, limit=limit)
        if limit == 1:
            (package / "SKILL.md").unlink()
            (package / "SKILL.md").symlink_to(outside)
        return entries, overflow

    monkeypatch.setattr(skills_loader, "_bounded_entry_names", swap_after_listing)

    registry = discover(user_dir=tmp_path)

    assert registry.rejections[0].name == "swapped"
    assert "symlink" in registry.rejections[0].error


def test_fifo_skill_file_is_rejected_without_blocking(tmp_path):
    package = tmp_path / "fifo-skill"
    package.mkdir()
    os.mkfifo(package / "SKILL.md")

    registry = discover(user_dir=tmp_path)

    assert registry.rejections[0].name == "fifo-skill"
    assert "regular file" in registry.rejections[0].error


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


def test_plugin_skills_are_added_without_overriding_existing_names(tmp_path):
    write_skill(tmp_path, "existing")
    unique = Skill(
        "plugin-guide",
        "Plugin guide.",
        "plugin guide",
        SkillSource.MCP_PLUGIN,
        Path("/plugins/acme/skills/plugin-guide/SKILL.md"),
        "acme",
    )
    conflict = Skill(
        "existing",
        "Conflicting guide.",
        "conflict",
        SkillSource.MCP_PLUGIN,
        Path("/plugins/acme/skills/existing/SKILL.md"),
        "acme",
    )

    snapshot = discover(user_dir=tmp_path, plugin_skills=(unique, conflict))

    assert snapshot.load("plugin-guide") == unique
    assert snapshot.load("existing").source is SkillSource.USER
    assert [(item.name, item.provider) for item in snapshot.rejections] == [("existing", "acme")]


def test_duplicate_plugin_skill_names_register_neither_definition(tmp_path):
    skills = tuple(
        Skill(
            "shared-guide",
            f"Guide from {provider}.",
            provider,
            SkillSource.MCP_PLUGIN,
            Path(f"/plugins/{provider}/skills/shared-guide/SKILL.md"),
            provider,
        )
        for provider in ("acme", "other")
    )

    snapshot = discover(user_dir=tmp_path, plugin_skills=skills)

    with pytest.raises(UnknownSkill):
        snapshot.load("shared-guide")
    assert {item.provider for item in snapshot.rejections} == {"acme", "other"}


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

    assert [skill.name for skill in registry.skills] == [
        "theater-configure",
        "theater-debate",
        "theater-orchestrate",
        "theater-recover-tmux",
    ]
    assert registry.rejections == ()


def test_overfull_user_root_is_rejected_wholesale(tmp_path):
    for number in range(SKILL_MAX_COUNT + 1):
        write_skill(tmp_path, f"skill-{number}")

    registry = discover(user_dir=tmp_path)

    assert all(skill.source is SkillSource.BUILTIN for skill in registry.skills)
    diagnostics = [
        (rejection.name, rejection.source_path, rejection.error)
        for rejection in registry.rejections
    ]
    assert diagnostics == [
        (None, tmp_path, f"skill root exceeds the limit of {SKILL_MAX_COUNT} entries")
    ]


def test_overfull_builtin_root_is_fatal(tmp_path):
    builtin = tmp_path / "builtin"
    for number in range(SKILL_MAX_COUNT + 1):
        write_skill(builtin, f"skill-{number}")

    with pytest.raises(BuiltinSkillError, match="exceeds the limit"):
        discover(builtin_dir=builtin, user_dir=tmp_path / "empty")


def test_discovery_returns_a_fresh_snapshot(tmp_path):
    write_skill(tmp_path, "alpha")

    registry = discover(user_dir=tmp_path)

    write_skill(tmp_path, "later")
    refreshed = discover(user_dir=tmp_path)
    assert all(skill.name != "later" for skill in registry.skills)
    assert refreshed.load("later").source is SkillSource.USER


@pytest.mark.parametrize("limit", [1, SKILL_MAX_COUNT])
def test_bounded_entry_enumeration_stops_after_the_limit(tmp_path, monkeypatch, limit):
    seen = 0

    class Entries:
        def __init__(self, fd):
            self.fd = fd

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            os.close(self.fd)
            return False

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal seen
            seen += 1
            if seen > limit + 1:
                raise AssertionError("enumeration exceeded the bounded cap")
            return SimpleNamespace(name=f"entry-{seen}")

    def fake_scandir(fd):
        return Entries(fd)

    monkeypatch.setattr(skills_loader, "_open_scandir", fake_scandir)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        entries, overflow = skills_loader._bounded_entry_names(root_fd, limit=limit)
    finally:
        os.close(root_fd)

    assert overflow is True
    assert len(entries) == limit + 1
    assert seen == limit + 1


def test_bounded_entry_enumeration_closes_its_duplicate_fd(tmp_path, monkeypatch):
    opened: list[int] = []
    original = skills_loader._open_scandir

    def capture(fd):
        opened.append(fd)
        return original(fd)

    monkeypatch.setattr(skills_loader, "_open_scandir", capture)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        skills_loader._bounded_entry_names(root_fd, limit=1)
    finally:
        os.close(root_fd)

    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_direct_registry_construction_sorts_skills_by_name():
    alpha = Skill("alpha", "a", "alpha", SkillSource.USER, Path("/tmp/alpha/SKILL.md"))
    zebra = Skill("zebra", "z", "zebra", SkillSource.USER, Path("/tmp/zebra/SKILL.md"))

    registry = SkillRegistry({"zebra": zebra, "alpha": alpha}, ())

    assert [skill.name for skill in registry.skills] == ["alpha", "zebra"]


@pytest.mark.parametrize(("name", "expected"), [("alpha", True), ("not/a-skill", False)])
def test_canonical_name_predicate_is_public(name, expected):
    assert is_canonical_name(name) is expected


def test_ensure_home_creates_the_skills_directory(monkeypatch, tmp_path):
    root = tmp_path / "state"
    monkeypatch.setenv("THEATER_HOME", str(root))

    paths.ensure_home()

    assert paths.skills_dir() == root / "skills"
    assert paths.skills_dir().is_dir()


async def test_skill_rpc_serializes_snapshots_and_maps_load_errors(monkeypatch):
    skill = Skill(
        "alpha",
        "A useful skill.",
        "---\nname: alpha\ndescription: A useful skill.\n---\n\n# Alpha\n",
        SkillSource.USER,
        Path("/tmp/skills/alpha/SKILL.md"),
    )
    rejection = SkillRejection(
        SkillSource.USER,
        Path("/tmp/skills/broken"),
        "broken",
        "frontmatter must contain exactly name and description",
    )
    plugin_skill = Skill(
        "plugin-guide",
        "Plugin guide.",
        "# Plugin guide\n",
        SkillSource.MCP_PLUGIN,
        Path("/tmp/plugins/acme/skills/plugin-guide/SKILL.md"),
        "acme",
    )
    snapshot = SkillRegistry({"alpha": skill, "plugin-guide": plugin_skill}, (rejection,))
    discoveries = []

    def fake_discover(*, plugin_skills=()):
        assert tuple(plugin_skills) == (plugin_skill,)
        discoveries.append(None)
        return snapshot

    monkeypatch.setattr(skills_rpc.registry, "discover", fake_discover)
    monkeypatch.setattr(
        skills_rpc.mcp_registry,
        "catalog",
        lambda: SimpleNamespace(registered_skills=(plugin_skill,)),
    )

    assert await skills_rpc._skills_list(None, {"ignored": True}) == {
        "skills": [
            {"name": "alpha", "description": "A useful skill.", "source": "user"},
            {
                "name": "plugin-guide",
                "description": "Plugin guide.",
                "source": "mcp_plugin",
                "plugin": "acme",
            },
        ],
        "rejections": [
            {
                "source": "user",
                "path": "/tmp/skills/broken",
                "name": "broken",
                "error": "frontmatter must contain exactly name and description",
            }
        ],
    }
    assert await skills_rpc._skills_load(None, {"name": "alpha"}) == {
        "name": "alpha",
        "description": "A useful skill.",
        "source": "user",
        "content": skill.content,
    }
    assert len(discoveries) == 2

    with pytest.raises(BadRequest, match=r"skills\.list"):
        await skills_rpc._skills_load(None, {"name": "not/a-skill"})
    with pytest.raises(BadRequest, match="non-empty string"):
        await skills_rpc._skills_load(None, {"name": ""})
    with pytest.raises(NotFound, match=r"skills\.list"):
        await skills_rpc._skills_load(None, {"name": "missing"})
