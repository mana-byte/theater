"""Production local catalog loading keeps package isolation and stage policy."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support.plugin_packages import MANIFEST_BODY
from theater.mcp_plugins import loading as mcp_loading
from theater.mcp_plugins.loading import SHIPPED
from theater.mcp_plugins.loading import scan as scan_shipped_mcp
from theater.plugins import catalog as user_catalog
from theater.plugins.catalog import scan as scan_user_plugins
from theater.plugins.loading import LOCAL, cleanup_package, synthetic_package_name

MCP_BODY = """
from .helper import VALUE
from theater.mcp_plugins import (
    MANIFEST_API_VERSION, McpLaunchManifest, McpLaunchPlan,
    McpServerManifest, PluginCapability,
)
MANIFEST = McpServerManifest(
    api_version=MANIFEST_API_VERSION,
    description=VALUE,
    capabilities=frozenset({{PluginCapability.PARTICIPANTS_READ}}),
    launch=McpLaunchManifest(planner=lambda _: McpLaunchPlan(command='fixture')),
    skills={skills!r},
    plays={plays!r},
)
"""


def _package(root: Path, name: str, value: str, *, skills=(), plays=()) -> Path:
    package = root / name
    package.mkdir(parents=True)
    (package / "helper.py").write_text(f"VALUE = {value!r}\n")
    (package / "manifest.py").write_text(MCP_BODY.format(skills=skills, plays=plays))
    return package


def test_local_catalog_isolates_same_named_siblings(tmp_path):
    root = tmp_path / "plugins"
    _package(root, "alpha", "from-alpha")
    _package(root, "beta", "from-beta")

    catalog = scan_user_plugins(root)

    assert [(item.name, item.manifest.description) for item in catalog.mcp_servers] == [
        ("alpha", "from-alpha"),
        ("beta", "from-beta"),
    ]
    assert catalog.rejected == ()


def test_local_catalog_cleans_post_import_failure_and_keeps_skill_first_order(tmp_path):
    root = tmp_path / "plugins"
    package = _package(root, "broken", "Broken", skills=("missing-skill",), plays=("missing-play",))
    (package / "skills").mkdir()

    catalog = scan_user_plugins(root)

    assert catalog.mcp_servers == ()
    assert len(catalog.rejected) == 1
    assert "missing-skill" in catalog.rejected[0].error
    assert "missing-play" not in catalog.rejected[0].error
    prefix = synthetic_package_name(package, LOCAL, prefix="theater_user_plugin_pkg_")
    assert not any(key == prefix or key.startswith(prefix + ".") for key in sys.modules)

    shipped = scan_shipped_mcp(root, source=SHIPPED)
    assert len(shipped) == 1
    assert "cannot inspect play package" in shipped[0].error
    assert "missing-skill" not in shipped[0].error


def test_local_catalog_cleans_harness_compilation_failure(tmp_path):
    root = tmp_path / "plugins"
    package = root / "broken"
    package.mkdir(parents=True)
    (package / "helper.py").write_text("VALUE = 1\n")
    (package / "manifest.py").write_text(
        "from .helper import VALUE\n"
        + MANIFEST_BODY.format(binary="broken", icon='""', aliases="()")
    )

    catalog = scan_user_plugins(root)

    assert catalog.harnesses == ()
    assert len(catalog.rejected) == 1
    assert "icon" in catalog.rejected[0].error
    prefix = synthetic_package_name(package, LOCAL, prefix="theater_user_plugin_pkg_")
    assert not any(key == prefix or key.startswith(prefix + ".") for key in sys.modules)


@pytest.mark.parametrize("failure", [RuntimeError("stage boom"), KeyboardInterrupt("stage boom")])
def test_local_catalog_post_import_exceptions_clean_package(tmp_path, monkeypatch, failure):
    root = tmp_path / "plugins"
    package = _package(root, "broken", "Broken")

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(user_catalog, "load_declared_skills", fail)
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt, match="stage boom"):
            scan_user_plugins(root)
    else:
        result = scan_user_plugins(root)
        assert "RuntimeError('stage boom')" in result.rejected[0].error
    prefix = synthetic_package_name(package, LOCAL, prefix="theater_user_plugin_pkg_")
    assert not any(key == prefix or key.startswith(prefix + ".") for key in sys.modules)


def test_shipped_mcp_unexpected_play_failure_keeps_current_exception_scope(tmp_path, monkeypatch):
    root = tmp_path / "shipped"
    package = _package(root, "broken", "Broken")

    def fail(*_args, **_kwargs):
        raise RuntimeError("play stage boom")

    monkeypatch.setattr(mcp_loading, "load_declared_plays", fail)
    prefix = synthetic_package_name(package, SHIPPED, prefix=mcp_loading.PACKAGE_PREFIX)
    try:
        with pytest.raises(RuntimeError, match="play stage boom"):
            scan_shipped_mcp(root, source=SHIPPED)
        assert prefix in sys.modules
    finally:
        cleanup_package(prefix)
