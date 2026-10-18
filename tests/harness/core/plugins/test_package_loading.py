"""Plugin package discovery, isolated import, error reporting and rescans."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from theater.harness.contracts.harness import Harness
from theater.harness.loading import LOCAL, SHIPPED, discover, load_plugin, scan
from theater.harness.loading.discovery import MANIFEST_FILENAME
from theater.harness.loading.importer import PACKAGE_PREFIX

MANIFEST_BODY = """
from theater.harness.contracts.callbacks import LaunchContext, ScreenContext
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    HarnessManifest,
    LaunchManifest,
    ObservationManifest,
    ScreenManifest,
)
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind, ScreenReading

def _plan(context):
    return LaunchPlan(argv=["{binary}", context.participant_id])

def _screen(_context):
    return ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH)

MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION,
    binary="{binary}",
    icon="@",
    launch=LaunchManifest(planner=_plan, approvals=frozenset({{"manual"}})),
    observation=ObservationManifest(
        primary=None,
        screen=ScreenManifest(classifier=_screen),
    ),
)
"""


def write_manifest(directory: Path, *, binary: str = "acme") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / MANIFEST_FILENAME
    manifest_path.write_text(MANIFEST_BODY.format(binary=binary), encoding="utf-8")
    return manifest_path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    d = tmp_path / "plugins"
    d.mkdir()
    return d


@pytest.mark.parametrize("layout", ["missing", "empty"])
def test_a_root_without_plugins_discovers_nothing(tmp_path: Path, layout: str) -> None:
    root = tmp_path / "plugins"
    if layout == "empty":
        root.mkdir()
    assert discover(root, source=LOCAL) == []


def test_deterministic_directory_name_order(root: Path) -> None:
    write_manifest(root / "zeta")
    write_manifest(root / "alpha")
    write_manifest(root / "mid")
    results = discover(root, source=LOCAL)
    assert [r.name for r in results] == ["alpha", "mid", "zeta"]


@pytest.mark.parametrize("name", [".hidden", "_internal"], ids=["hidden", "underscored"])
def test_hidden_and_underscored_directories_are_ignored(root: Path, name: str) -> None:
    write_manifest(root / name)
    write_manifest(root / "acme")
    results = discover(root, source=LOCAL)
    assert [r.name for r in results] == ["acme"]


def test_non_canonical_directory_name_is_broken_not_ignored(root: Path) -> None:
    bad = root / "Upper"
    bad.mkdir()
    (bad / MANIFEST_FILENAME).write_text("raise RuntimeError('must not execute')")
    write_manifest(root / "acme")
    results = discover(root, source=LOCAL)
    names = [r.name for r in results]
    assert "Upper" in names
    assert "acme" in names
    upper = next(r for r in results if r.name == "Upper")
    assert upper.harness is None
    assert upper.error is not None
    assert "not a valid harness name" in upper.error
    assert "must not execute" not in upper.error


def test_visible_directory_without_manifest_is_broken(root: Path) -> None:
    (root / "acme").mkdir()
    results = discover(root, source=LOCAL)
    assert len(results) == 1
    assert results[0].name == "acme"
    assert results[0].harness is None
    assert results[0].error is not None
    assert MANIFEST_FILENAME in results[0].error


def test_non_py_files_are_ignored(root: Path) -> None:
    (root / "notes.txt").write_text("not a plugin")
    (root / "acme.json").write_text("{}")
    write_manifest(root / "acme")
    results = discover(root, source=LOCAL)
    assert [r.name for r in results] == ["acme"]


def test_legacy_top_level_py_is_never_executed(root: Path) -> None:
    legacy = root / "acme.py"
    legacy.write_text("raise RuntimeError('must not execute')")
    results = discover(root, source=LOCAL)
    assert len(results) == 1
    assert results[0].name == "acme"
    assert results[0].harness is None
    assert results[0].error is not None
    assert "legacy" in results[0].error
    assert "manifest.py" in results[0].error


def test_legacy_non_py_files_ignored(root: Path) -> None:
    (root / "acme.txt").write_text("not a plugin")
    (root / "data.json").write_text("{}")
    assert discover(root, source=LOCAL) == []


def test_disabled_name_filtered_before_import(root: Path) -> None:
    bomb = root / "acme" / MANIFEST_FILENAME
    bomb.parent.mkdir()
    bomb.write_text("raise RuntimeError('must not execute')")
    results = discover(root, source=LOCAL, skip={"acme"})
    assert results == []


def test_disabled_manifest_raises_is_never_executed(root: Path) -> None:
    bomb_dir = root / "bomb"
    bomb_dir.mkdir()
    (bomb_dir / MANIFEST_FILENAME).write_text("raise RuntimeError('boom')")
    results = scan(root, source=LOCAL, skip={"bomb"})
    assert results == []
    from theater.harness.loading.importer import _synthetic_package_name

    pkg = _synthetic_package_name(bomb_dir, LOCAL)
    assert not any(k == pkg or k.startswith(pkg + ".") for k in sys.modules)


def test_valid_manifest_compiles_into_harness(root: Path) -> None:
    write_manifest(root / "acme")
    results = scan(root, source=LOCAL)
    assert len(results) == 1
    r = results[0]
    assert r.name == "acme"
    assert r.error is None
    assert isinstance(r.harness, Harness)
    assert r.harness.name == "acme"
    assert r.harness.binary == "acme"


def test_shipped_source_takes_same_path(root: Path) -> None:
    write_manifest(root / "acme")
    results = scan(root, source=SHIPPED)
    assert len(results) == 1
    assert results[0].source == SHIPPED
    assert isinstance(results[0].harness, Harness)


@pytest.mark.parametrize(
    ("import_line", "support_files", "value"),
    [
        pytest.param(
            "from .parser import VALUE", {"parser.py": "VALUE = 42\n"}, "42", id="relative_sibling"
        ),
        pytest.param(
            "from .helpers.util import VALUE",
            {"helpers/__init__.py": "", "helpers/util.py": "VALUE = 99\n"},
            "99",
            id="nested_package",
        ),
    ],
)
def test_manifests_import_their_own_siblings(
    root: Path, import_line: str, support_files: dict[str, str], value: str
) -> None:
    acme = root / "acme"
    for relative, content in support_files.items():
        (acme / relative).parent.mkdir(parents=True, exist_ok=True)
        (acme / relative).write_text(content)
    (acme / MANIFEST_FILENAME).write_text(
        MANIFEST_BODY.format(binary="acme")
        .replace("def _plan(context):", f"{import_line}\ndef _plan(context):")
        .replace('argv=["acme", context.participant_id]', 'argv=["acme", str(VALUE)]'),
        encoding="utf-8",
    )
    results = scan(root, source=LOCAL)
    assert len(results) == 1
    assert results[0].harness is not None
    assert results[0].harness.plan_launch(
        participant_id="p", prompt="hi", config_path=Path("/c"), approval="manual"
    ).argv == ["acme", value]


def test_same_named_siblings_in_separate_sources_do_not_collide(root: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    for d, val in [(root, "root"), (other, "other")]:
        pkg = d / "acme"
        pkg.mkdir()
        (pkg / "helper.py").write_text(f"ORIGIN = {val!r}\n")
        (pkg / MANIFEST_FILENAME).write_text(
            MANIFEST_BODY.format(binary="acme")
            .replace(
                "def _plan(context):",
                "from .helper import ORIGIN\ndef _plan(context):",
            )
            .replace(
                'argv=["acme", context.participant_id]',
                'argv=["acme", ORIGIN]',
            ),
            encoding="utf-8",
        )
    r1 = scan(root, source=LOCAL)
    r2 = scan(other, source=SHIPPED)
    assert r1[0].harness is not None
    assert r2[0].harness is not None
    assert r1[0].harness.plan_launch(
        participant_id="p", prompt="hi", config_path=Path("/c"), approval="manual"
    ).argv == ["acme", "root"]
    assert r2[0].harness.plan_launch(
        participant_id="p", prompt="hi", config_path=Path("/c"), approval="manual"
    ).argv == ["acme", "other"]


def test_no_sys_path_pollution(root: Path) -> None:
    write_manifest(root / "acme")
    scan(root, source=LOCAL)
    assert str(root / "acme") not in sys.path
    assert str(root) not in sys.path


def test_no_stdlib_shadowing(root: Path) -> None:
    acme = root / "acme"
    acme.mkdir()
    (acme / "json.py").write_text("VALUE = 'shadowed'\n")
    (acme / MANIFEST_FILENAME).write_text(
        MANIFEST_BODY.format(binary="acme")
        .replace(
            "def _plan(context):",
            "import json as _json\nfrom .json import VALUE\ndef _plan(context):",
        )
        .replace(
            'argv=["acme", context.participant_id]',
            'argv=["acme", VALUE]',
        ),
        encoding="utf-8",
    )
    results = scan(root, source=LOCAL)
    assert len(results) == 1
    assert results[0].harness is not None
    # stdlib json must not be replaced
    import json

    assert sys.modules["json"] is json


@pytest.mark.parametrize(
    ("body", "fragments"),
    [
        pytest.param("x = 1\n", ["no MANIFEST", MANIFEST_FILENAME], id="missing_export"),
        pytest.param(
            "from theater.harness.contracts.manifest import HarnessManifest\n"
            "MANIFEST = HarnessManifest\n",
            ["not an instance"],
            id="class_instead_of_instance",
        ),
        pytest.param("MANIFEST = 'a string'\n", ["is not a HarnessManifest"], id="wrong_type"),
    ],
)
def test_a_module_without_a_manifest_instance_is_broken(
    root: Path, body: str, fragments: list[str]
) -> None:
    acme = root / "acme"
    acme.mkdir()
    (acme / MANIFEST_FILENAME).write_text(body)
    results = scan(root, source=LOCAL)
    assert len(results) == 1
    assert results[0].harness is None
    assert all(fragment in results[0].error for fragment in fragments)


@pytest.mark.parametrize(
    ("body", "fragments"),
    [
        pytest.param("raise ValueError('boom')", ["boom", MANIFEST_FILENAME], id="import_failure"),
        pytest.param("def (:", [MANIFEST_FILENAME], id="syntax_error"),
        pytest.param("import sys; sys.exit(1)", [], id="system_exit"),
    ],
)
def test_a_manifest_that_fails_to_import_is_broken(
    root: Path, body: str, fragments: list[str]
) -> None:
    acme = root / "acme"
    acme.mkdir()
    (acme / MANIFEST_FILENAME).write_text(body)
    results = scan(root, source=LOCAL)
    assert len(results) == 1
    assert results[0].harness is None
    assert results[0].error is not None
    assert all(fragment in results[0].error for fragment in fragments)


def test_failed_import_leaves_no_modules(root: Path) -> None:
    acme = root / "acme"
    acme.mkdir()
    (acme / "helper.py").write_text("VALUE = 1\n")
    (acme / MANIFEST_FILENAME).write_text("from .helper import VALUE\nraise ValueError('boom')\n")
    scan(root, source=LOCAL)
    from theater.harness.loading.importer import _synthetic_package_name

    pkg = _synthetic_package_name(acme, LOCAL)
    leaked = [k for k in sys.modules if k == pkg or k.startswith(pkg + ".")]
    assert leaked == []


def test_successful_import_caches_synthetic_package(root: Path) -> None:
    write_manifest(root / "acme")
    results = scan(root, source=LOCAL)
    assert results[0].harness is not None
    cached = [k for k in sys.modules if k.startswith(PACKAGE_PREFIX)]
    assert len(cached) >= 1


def test_keyboard_interrupt_propagates_and_cleans_up(root: Path) -> None:
    acme = root / "acme"
    acme.mkdir()
    (acme / MANIFEST_FILENAME).write_text("raise KeyboardInterrupt('ctrl-c')")
    with pytest.raises(KeyboardInterrupt):
        scan(root, source=LOCAL)
    from theater.harness.loading.importer import _synthetic_package_name

    pkg = _synthetic_package_name(acme, LOCAL)
    leaked = [k for k in sys.modules if k == pkg or k.startswith(pkg + ".")]
    assert leaked == []


def test_repeat_scan_observes_edits(root: Path) -> None:
    write_manifest(root / "acme", binary="acme")
    first = scan(root, source=LOCAL)
    assert first[0].harness is not None
    assert first[0].harness.binary == "acme"
    write_manifest(root / "acme", binary="changed")
    second = scan(root, source=LOCAL)
    assert second[0].harness is not None
    assert second[0].harness.binary == "changed"


def test_rescan_of_formerly_successful_manifest_now_raising(root: Path) -> None:
    acme = root / "acme"
    write_manifest(acme)
    first = scan(root, source=LOCAL)
    assert first[0].harness is not None
    (acme / MANIFEST_FILENAME).write_text("raise ValueError('now broken')\n")
    second = scan(root, source=LOCAL)
    assert second[0].harness is None
    assert "now broken" in second[0].error
    from theater.harness.loading.importer import _synthetic_package_name

    pkg = _synthetic_package_name(acme, LOCAL)
    leaked = [k for k in sys.modules if k == pkg or k.startswith(pkg + ".")]
    assert leaked == []


def test_init_py_is_not_executed_automatically(root: Path) -> None:
    acme = root / "acme"
    acme.mkdir()
    (acme / "__init__.py").write_text("raise RuntimeError('init must not run')")
    write_manifest(acme)
    results = scan(root, source=LOCAL)
    assert len(results) == 1
    assert results[0].harness is not None


def test_disabled_legacy_file_stem_not_executed(root: Path) -> None:
    legacy = root / "acme.py"
    legacy.write_text("raise RuntimeError('must not execute')")
    results = scan(root, source=LOCAL, skip={"acme"})
    assert results == []


def test_validation_error_is_prefixed_with_manifest_path(root: Path) -> None:
    acme = root / "acme"
    acme.mkdir()
    (acme / MANIFEST_FILENAME).write_text(
        MANIFEST_BODY.format(binary="acme").replace('icon="@"', 'icon=""')
    )
    results = scan(root, source=LOCAL)
    assert len(results) == 1
    assert results[0].harness is None
    assert results[0].error is not None
    assert MANIFEST_FILENAME in results[0].error
    assert "icon" in results[0].error


def test_pre_existing_error_passthrough(root: Path) -> None:
    (root / "acme").mkdir()
    raw = discover(root, source=LOCAL)
    assert raw[0].error is not None
    result = load_plugin(raw[0])
    assert result.error == raw[0].error
    assert result.harness is None
