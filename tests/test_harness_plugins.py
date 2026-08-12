"""Harness adapters loaded from `$THEATER_HOME/harnesses/*.py`.

The tests that matter here are the refusals. A plugin is arbitrary Python that
runs in the daemon, so the loader's job is not to be clever — it is to make
every way of getting it wrong say so, with the file path, instead of leaving a
harness quietly missing from a registry the user believes they extended.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theater import cli
from theater import config as cfg
from theater import harness as harness_registry
from theater import paths
from theater.harness import plugins
from theater.harness.plugins import PluginError

#: A plugin that satisfies the ABC with the least code that can work. Every
#: test that is not about a specific method starts from this.
BODY = '''
from pathlib import Path

from theater.harness import Harness, LaunchPlan


class {cls}(Harness):
    name = "{name}"
    binary = "{binary}"
    icon = "{icon}"
    aliases = {aliases}

    def plan_launch(self, *, participant_id, prompt, config_path, approval):
        return LaunchPlan(argv=[self.binary, prompt], env={{"ID": participant_id}})

    def find_transcript(self, *, cwd, session_id=None, after=None):
        return None

    def session_id(self, transcript):
        return None

    def parse(self, line, index, *, clip_text=True):
        return []

    def native_children(self, transcript):
        return []

    def is_idle_screen(self, capture):
        return capture.endswith("> ")


HARNESS = {cls}()
'''


def plugin(
    dirpath: Path,
    filename: str = "codex.py",
    *,
    cls: str = "CodexHarness",
    name: str = "codex",
    binary: str = "codex",
    icon: str = "@",
    aliases: tuple[str, ...] = (),
) -> Path:
    path = dirpath / filename
    path.write_text(
        BODY.format(
            cls=cls, name=name, binary=binary, icon=icon, aliases=repr(aliases)
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def plugin_dir(tmp_path) -> Path:
    d = tmp_path / "harnesses"
    d.mkdir()
    return d


def install(plugin_dir: Path, config: cfg.Config | None = None) -> list[str]:
    return harness_registry.install(config or cfg.Config(), plugin_dir=plugin_dir)


# ---- loading ------------------------------------------------------------


def test_a_plugin_joins_the_registry(plugin_dir):
    plugin(plugin_dir)
    assert install(plugin_dir) == ["codex"]
    assert harness_registry.get("codex").binary == "codex"
    assert harness_registry.harness_icon("codex") == "@"


def test_a_plugin_is_a_full_adapter(plugin_dir):
    """The whole point of a plugin over a declaration: it can parse."""
    plugin(plugin_dir)
    install(plugin_dir)
    assert harness_registry.get("codex").has_transcript is True


def test_a_plugin_plans_its_own_launch(plugin_dir):
    plugin(plugin_dir)
    install(plugin_dir)
    plan = harness_registry.plan_launch(
        "codex",
        participant_id="abc123",
        prompt="hello",
        config_path=Path("/tmp/x.json"),
        approval="manual",
    )
    assert plan.argv == ["codex", "hello"]
    assert plan.env == {"ID": "abc123"}


def test_a_missing_directory_is_not_an_error(tmp_path):
    assert plugins.load(tmp_path / "nope") == []


def test_an_empty_directory_leaves_the_builtins(plugin_dir):
    assert install(plugin_dir) == []
    assert set(harness_registry.HARNESSES) == {"claude", "vibe"}


def test_underscored_files_are_skipped(plugin_dir):
    """So a plugin can keep a helper module beside it."""
    (plugin_dir / "_shared.py").write_text("raise RuntimeError('imported')")
    plugin(plugin_dir)
    assert install(plugin_dir) == ["codex"]


def test_non_python_files_are_ignored(plugin_dir):
    (plugin_dir / "notes.txt").write_text("not a plugin")
    (plugin_dir / "codex.py.bak").write_text("also not")
    plugin(plugin_dir)
    assert install(plugin_dir) == ["codex"]


def test_plugins_load_in_filename_order(plugin_dir):
    plugin(plugin_dir, "b.py", cls="B", name="bee", binary="bee")
    plugin(plugin_dir, "a.py", cls="A", name="ay", binary="ay")
    assert install(plugin_dir) == ["ay", "bee"]


def test_a_plugin_does_not_take_a_real_module_name(plugin_dir):
    """A user's `json.py` must not become *the* json module for the daemon."""
    import json
    import sys

    plugin(plugin_dir, "json.py", cls="JsonHarness", name="jsonish", binary="j")
    install(plugin_dir)
    assert sys.modules["json"] is json
    assert plugins.MODULE_PREFIX + "json" in sys.modules


def test_installing_twice_is_the_same_as_once(plugin_dir):
    plugin(plugin_dir)
    install(plugin_dir)
    assert install(plugin_dir) == ["codex"]
    assert sorted(harness_registry.HARNESSES) == ["claude", "codex", "vibe"]


def test_an_empty_config_restores_the_builtins(plugin_dir):
    plugin(plugin_dir)
    install(plugin_dir)
    harness_registry.install(cfg.Config())
    assert "codex" not in harness_registry.HARNESSES


# ---- refusals -----------------------------------------------------------


def test_a_plugin_that_raises_on_import_names_the_file(plugin_dir):
    bad = plugin_dir / "broken.py"
    bad.write_text("raise ValueError('boom')")
    with pytest.raises(PluginError, match="broken.py"):
        install(plugin_dir)


def test_a_plugin_with_a_syntax_error_names_the_file(plugin_dir):
    bad = plugin_dir / "broken.py"
    bad.write_text("def (:")
    with pytest.raises(PluginError, match="broken.py"):
        install(plugin_dir)


def test_a_plugin_that_fails_to_import_leaves_no_module_behind(plugin_dir):
    """A half-executed module in sys.modules would be found by the next import."""
    import sys

    (plugin_dir / "broken.py").write_text("raise ValueError('boom')")
    with pytest.raises(PluginError):
        install(plugin_dir)
    assert plugins.MODULE_PREFIX + "broken" not in sys.modules


def test_a_plugin_with_no_harness_says_what_to_write(plugin_dir):
    (plugin_dir / "empty.py").write_text("x = 1\n")
    with pytest.raises(PluginError, match="HARNESS = MyHarness"):
        install(plugin_dir)


def test_exporting_the_class_instead_of_an_instance_is_caught(plugin_dir):
    """The likeliest mistake, and the error tells you the fix verbatim."""
    body = BODY.format(
        cls="CodexHarness", name="codex", binary="codex", icon="@", aliases="()"
    ).replace("HARNESS = CodexHarness()", "HARNESS = CodexHarness")
    (plugin_dir / "codex.py").write_text(body)
    with pytest.raises(PluginError, match=r"CodexHarness\(\)"):
        install(plugin_dir)


def test_a_harness_that_is_not_a_harness_is_caught(plugin_dir):
    (plugin_dir / "odd.py").write_text("HARNESS = 'a string'\n")
    with pytest.raises(PluginError, match="does not subclass"):
        install(plugin_dir)


def test_an_illegal_name_is_caught(plugin_dir):
    plugin(plugin_dir, name="My Codex")
    with pytest.raises(PluginError, match="lowercase letters"):
        install(plugin_dir)


def test_a_missing_binary_is_caught(plugin_dir):
    plugin(plugin_dir, binary="")
    with pytest.raises(PluginError, match="no binary"):
        install(plugin_dir)


def test_a_multi_character_icon_is_caught(plugin_dir):
    """Two columns wide would shift every row of every listing."""
    plugin(plugin_dir, icon="<>")
    with pytest.raises(PluginError, match="one character"):
        install(plugin_dir)


def test_two_plugins_with_one_name_name_both_files(plugin_dir):
    plugin(plugin_dir, "one.py", cls="One")
    plugin(plugin_dir, "two.py", cls="Two")
    with pytest.raises(harness_registry.ConfigError) as exc:
        install(plugin_dir)
    assert "one.py" in str(exc.value) and "two.py" in str(exc.value)


# ---- precedence ---------------------------------------------------------


def test_a_plugin_may_replace_a_builtin(plugin_dir):
    """Unlike a declaration: a plugin can do everything the built-in did."""
    plugin(plugin_dir, "vibe.py", cls="MyVibe", name="vibe", binary="vibe")
    install(plugin_dir)
    assert type(harness_registry.get("vibe")).__name__ == "MyVibe"
    # The built-in's aliases still resolve, and still to the same name.
    assert harness_registry.normalize("mistral-vibe") == "vibe"


def test_a_declaration_may_not_replace_a_plugin(plugin_dir):
    plugin(plugin_dir)
    config = declared_config("codex")
    with pytest.raises(harness_registry.ConfigError, match="both"):
        install(plugin_dir, config)


def test_a_declaration_and_a_plugin_coexist(plugin_dir):
    plugin(plugin_dir)
    install(plugin_dir, declared_config("opencode"))
    assert sorted(harness_registry.HARNESSES) == [
        "claude",
        "codex",
        "opencode",
        "vibe",
    ]


def test_a_plugin_alias_normalizes(plugin_dir):
    plugin(plugin_dir, aliases=("codex-cli",))
    install(plugin_dir)
    assert harness_registry.normalize("codex-cli") == "codex"


def test_a_plugin_alias_cannot_shadow_another_harness(plugin_dir):
    plugin(plugin_dir, aliases=("mistral-vibe",))
    with pytest.raises(harness_registry.ConfigError, match="already resolves"):
        install(plugin_dir)


def test_a_plugin_alias_cannot_be_another_harness_name(plugin_dir):
    plugin(plugin_dir, aliases=("claude",))
    with pytest.raises(harness_registry.ConfigError, match="name of another"):
        install(plugin_dir)


def test_a_declaration_cannot_take_a_plugin_alias(plugin_dir):
    plugin(plugin_dir, aliases=("oc",))
    config = declared_config("opencode", aliases=["oc"])
    with pytest.raises(harness_registry.ConfigError, match="already resolves"):
        install(plugin_dir, config)


def declared_config(name: str, **overrides) -> cfg.Config:
    spec = cfg.HarnessSpec(
        binary=name,
        idle_prompts=[">"],
        approvals={"manual": [], "edits": [], "yolo": []},
        **overrides,
    )
    return cfg.Config(harnesses={name: spec})


# ---- the rest of the system sees them -----------------------------------


def test_the_cli_loads_plugins_from_theater_home(capsys):
    """`ensure_home` makes the directory; the CLI reads it before dispatching."""
    paths.ensure_home()
    plugin(paths.harnesses_dir())
    assert cli.main(["harnesses"]) == 0
    out = capsys.readouterr().out
    assert "codex" in out


def test_a_broken_plugin_stops_the_cli(capsys):
    paths.ensure_home()
    (paths.harnesses_dir() / "broken.py").write_text("raise ValueError('boom')")
    assert cli.main(["ls"]) == 1
    assert "broken.py" in capsys.readouterr().err


def test_ensure_home_creates_the_directory():
    paths.ensure_home()
    assert paths.harnesses_dir().is_dir()


def test_a_plugin_binary_joins_the_unmanaged_sweep(plugin_dir):
    plugin(plugin_dir, binary="codex-bin")
    install(plugin_dir)
    assert "codex-bin" in harness_registry.known_binaries()


def test_a_plugin_shows_up_in_describe(plugin_dir):
    plugin(plugin_dir)
    install(plugin_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["codex"]["icon"] == "@"
    assert rows["codex"]["binary"] == "codex"


def test_a_plugin_is_observable_from_its_transcript(plugin_dir):
    """`has_transcript` decides which watch loop the observer runs."""
    plugin(plugin_dir)
    install(plugin_dir)
    codex = harness_registry.get("codex")
    assert codex.has_transcript is True
    assert codex.is_idle_screen("something\n> ") is True
