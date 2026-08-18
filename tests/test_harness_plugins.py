"""Harness adapters loaded from plugin files.

Two directories, one loader: the adapters Theater ships and the ones a user
drops in `$THEATER_HOME/harnesses/`. The tests that matter here are the
refusals and the precedence. A plugin is arbitrary Python that runs in the
daemon, so the loader's job is not to be clever — it is to make every way of
getting it wrong say so, with the file path, instead of leaving a harness
quietly missing from a registry the user believes they extended.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theater import cli, paths
from theater import config as cfg
from theater import harness as harness_registry
from theater.harness import plugins
from theater.harness.plugins import PluginError

#: A plugin that satisfies both ABCs with the least code that can work. Every
#: test that is not about a specific method starts from this. Note the shape it
#: forces: a harness that launches, and a separate observer it carries, which
#: is the whole point of the v1.6 split.
BODY = """
from pathlib import Path

from theater.harness import Harness, LaunchPlan, TranscriptObserver


class {cls}Observer(TranscriptObserver):
    def find_transcript(self, *, cwd, session_id=None, after=None):
        return None

    def session_id(self, transcript):
        return None

    def parse(self, line, index, *, clip_text=True):
        return []

    def is_idle_screen(self, capture):
        return capture.endswith("> ")


class {cls}(Harness):
    name = "{name}"
    binary = "{binary}"
    icon = {icon}
    aliases = {aliases}

    def __init__(self):
        self.observer = {cls}Observer()

    def plan_launch(self, *, participant_id, prompt, config_path, approval):
        return LaunchPlan(argv=[self.binary, prompt], env={{"ID": participant_id}})


HARNESS = {cls}()
"""


def plugin(
    dirpath: Path,
    filename: str = "acme.py",
    *,
    cls: str = "AcmeHarness",
    name: str = "acme",
    binary: str = "acme",
    icon: str = "@",
    aliases: tuple[str, ...] = (),
) -> Path:
    path = dirpath / filename
    path.write_text(
        BODY.format(cls=cls, name=name, binary=binary, icon=repr(icon), aliases=repr(aliases)),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def local_dir(tmp_path) -> Path:
    """Stand-in for `$THEATER_HOME/harnesses`."""
    d = tmp_path / "harnesses"
    d.mkdir()
    return d


@pytest.fixture
def shipped_dir(tmp_path) -> Path:
    """Stand-in for the directory of adapters Theater ships.

    Only for the tests about *being* shipped — failing fatally, being
    overridden. Everything else installs against the real one, because
    "claude and vibe are in the registry" is the behaviour under test.
    """
    d = tmp_path / "shipped"
    d.mkdir()
    return d


def install(local: Path, config: cfg.Config | None = None, **kwargs) -> list[str]:
    return harness_registry.install(config or cfg.Config(), local_dir=local, **kwargs)


def disabling(*names: str) -> cfg.Config:
    return cfg.Config(harness=cfg.HarnessSection(disabled=list(names)))


def error_in(directory: Path) -> str:
    """The one load error in `directory`, as the loader reports it."""
    broken = [p for p in plugins.scan(directory, source=plugins.LOCAL) if p.error]
    assert len(broken) == 1, [p.name for p in broken]
    assert broken[0].error is not None
    return broken[0].error


# ---- loading ------------------------------------------------------------


def test_a_plugin_joins_the_registry(local_dir):
    plugin(local_dir)
    assert install(local_dir) == ["acme", "claude", "codex", "opencode", "vibe"]
    assert harness_registry.get("acme").binary == "acme"
    assert harness_registry.harness_icon("acme") == "@"


def test_the_shipped_adapters_are_plugins_too(local_dir):
    """No built-in tier: every adapter comes through the same loader."""
    assert install(local_dir) == ["claude", "codex", "opencode", "vibe"]
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["vibe"]["source"] == "shipped"
    assert rows["claude"]["source"] == "shipped"
    assert rows["codex"]["source"] == "shipped"
    assert rows["opencode"]["source"] == "shipped"


def test_a_plugin_plans_its_own_launch(local_dir):
    plugin(local_dir)
    install(local_dir)
    plan = harness_registry.plan_launch(
        "acme",
        participant_id="abc123",
        prompt="hello",
        config_path=Path("/tmp/x.json"),
        approval="manual",
    )
    assert plan.argv == ["acme", "hello"]
    assert plan.env == {"ID": "abc123"}


def test_a_missing_directory_is_not_an_error(tmp_path):
    assert plugins.scan(tmp_path / "nope", source=plugins.LOCAL) == []


def test_underscored_files_are_skipped(local_dir):
    """So a plugin can keep a helper module beside it."""
    (local_dir / "_shared.py").write_text("raise RuntimeError('imported')")
    plugin(local_dir)
    assert "acme" in install(local_dir)


def test_a_shared_helper_module_is_importable(local_dir):
    """C4: a ``_``-prefixed helper beside a plugin can be imported by it.

    A meta path finder resolves ``import _shared`` to ``_shared.py`` in the
    plugin's directory, loading it lazily under a mangled module name. No bare
    helper name survives in ``sys.modules`` after the plugin loads.
    """
    (local_dir / "_shared.py").write_text("VALUE = 42\n")
    (local_dir / "acme.py").write_text(
        BODY.format(
            cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
        ).replace(
            "class AcmeHarness(Harness):",
            "import _shared\nclass AcmeHarness(Harness):\n    _shared_value = _shared.VALUE",
        )
    )
    assert "acme" in install(local_dir)
    assert harness_registry.get("acme")._shared_value == 42


def test_a_shared_helper_does_not_leak_into_sys_modules(local_dir):
    """C4: the bare helper name must not remain in ``sys.modules`` after loading.

    If it did, a later plugin with its own ``_shared.py`` would silently get
    the first one's module.
    """
    import sys

    (local_dir / "_shared.py").write_text("VALUE = 42\n")
    (local_dir / "acme.py").write_text(
        BODY.format(
            cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
        ).replace(
            "class AcmeHarness(Harness):",
            "import _shared\nclass AcmeHarness(Harness):",
        )
    )
    install(local_dir)
    assert "_shared" not in sys.modules


def test_two_shared_helpers_in_two_sources_do_not_collide(local_dir, shipped_dir):
    """C4: each plugin sees its own helper, not the other source's.

    Two ``_shared.py`` files in two scanned directories, each exporting a
    different value. Without private-namespace loading, the first one wins for
    the whole process and the second plugin silently gets the wrong file.
    """
    (shipped_dir / "_shared.py").write_text('ORIGIN = "shipped"\n')
    (local_dir / "_shared.py").write_text('ORIGIN = "local"\n')
    (shipped_dir / "alpha.py").write_text(
        BODY.format(
            cls="AlphaHarness", name="alpha", binary="alpha", icon=repr("@"), aliases="()"
        ).replace(
            "class AlphaHarness(Harness):",
            "import _shared\nclass AlphaHarness(Harness):\n    _shared_origin = _shared.ORIGIN",
        )
    )
    (local_dir / "beta.py").write_text(
        BODY.format(
            cls="BetaHarness", name="beta", binary="beta", icon=repr("#"), aliases="()"
        ).replace(
            "class BetaHarness(Harness):",
            "import _shared\nclass BetaHarness(Harness):\n    _shared_origin = _shared.ORIGIN",
        )
    )
    install(local_dir, shipped_dir=shipped_dir)
    assert harness_registry.get("alpha")._shared_origin == "shipped"
    assert harness_registry.get("beta")._shared_origin == "local"


def test_a_shared_helper_executes_once_and_is_shared_between_plugins(local_dir):
    """C4: the mangled name caches the helper in ``sys.modules``, so two
    plugins in the same directory that import the same helper get the same
    module object and the helper's import-time code runs exactly once.

    Without mangling (``_helper_module_name`` returning the bare stem), each
    plugin would re-execute the helper and get a distinct module object —
    silently duplicating any state the helper holds (a client, a cache, a
    counter). This test pins the caching property the mangling provides.
    """
    (local_dir / "_counter.py").write_text("executions = [0]\nexecutions[0] += 1\n")
    (local_dir / "alpha.py").write_text(
        BODY.format(
            cls="AlphaHarness", name="alpha", binary="alpha", icon=repr("@"), aliases="()"
        ).replace(
            "class AlphaHarness(Harness):",
            "import _counter\nclass AlphaHarness(Harness):\n    "
            "_counter_executions = _counter.executions\n    "
            "_counter_module = _counter",
        )
    )
    (local_dir / "beta.py").write_text(
        BODY.format(
            cls="BetaHarness", name="beta", binary="beta", icon=repr("#"), aliases="()"
        ).replace(
            "class BetaHarness(Harness):",
            "import _counter\nclass BetaHarness(Harness):\n    "
            "_counter_executions = _counter.executions\n    "
            "_counter_module = _counter",
        )
    )
    install(local_dir)
    alpha = harness_registry.get("alpha")
    beta = harness_registry.get("beta")
    assert alpha._counter_executions == [1]
    assert beta._counter_executions == [1]
    assert alpha._counter_module is beta._counter_module


def test_a_broken_helper_names_the_helper_file_and_real_cause(local_dir):
    """C4: a helper with a syntax error must report the helper file and the
    real exception, not a misleading ``ModuleNotFoundError``.

    Without the fix, the pre-load loop swallowed the helper's failure and left
    the bare name uninjected, so the plugin's own ``import`` failed with
    ``ModuleNotFoundError`` — a message that says the file is missing when it
    is sitting right there with a syntax error.
    """
    (local_dir / "_broken.py").write_text("this is not valid python !!!\n")
    (local_dir / "acme.py").write_text(
        BODY.format(
            cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
        ).replace(
            "class AcmeHarness(Harness):",
            "import _broken\nclass AcmeHarness(Harness):",
        )
    )
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["acme"]["error"] is not None
    error = rows["acme"]["error"]
    assert "_broken.py" in error
    assert "SyntaxError" in error or "syntax" in error.lower()
    assert "ModuleNotFoundError" not in error


def test_a_nested_helper_import_works(local_dir):
    """C4: a helper importing another helper in the same directory works.

    ``_derived`` imports ``_base``. The meta path finder resolves both lazily:
    when the plugin imports ``_derived``, the finder loads ``_derived``, which
    triggers ``import _base``, which the finder also resolves. Neither bare
    name survives in ``sys.modules`` afterward.
    """
    import sys

    (local_dir / "_base.py").write_text('VALUE = "base"\n')
    (local_dir / "_derived.py").write_text("import _base\nVALUE = _base.VALUE + '+derived'\n")
    (local_dir / "acme.py").write_text(
        BODY.format(
            cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
        ).replace(
            "class AcmeHarness(Harness):",
            "import _derived\nclass AcmeHarness(Harness):\n    _derived_value = _derived.VALUE",
        )
    )
    assert "acme" in install(local_dir)
    assert harness_registry.get("acme")._derived_value == "base+derived"
    assert "_base" not in sys.modules
    assert "_derived" not in sys.modules


def test_a_helper_that_calls_sys_exit_does_not_poison_the_cache(local_dir):
    """R5-4: a helper calling ``sys.exit()`` raises ``SystemExit`` (a
    ``BaseException``), which ``except Exception`` does not catch. The
    half-executed helper would survive in ``sys.modules`` under its mangled
    name and a later scan would reuse it as if it had loaded successfully."""
    import sys

    (local_dir / "_bye.py").write_text("VALUE = 'partial'\nimport sys; sys.exit(1)\n")
    (local_dir / "acme.py").write_text(
        BODY.format(
            cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
        ).replace(
            "class AcmeHarness(Harness):",
            "import _bye\nclass AcmeHarness(Harness):",
        )
    )
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["acme"]["error"] is not None
    assert "_bye" in rows["acme"]["error"]
    # The mangled helper must not survive in sys.modules.
    leaked = [k for k in sys.modules if k.startswith(plugins.HELPER_PREFIX) and k.endswith("_bye")]
    assert leaked == [], f"half-executed helper survived: {leaked}"


def test_a_helper_shadowing_a_stdlib_module_is_refused(local_dir):
    """R5-1: a ``_``-prefixed helper whose bare name is already in
    ``sys.modules`` (e.g. ``_json``, ``_thread``) would silently be replaced
    by the pre-loaded module — the finder is never consulted because
    ``sys.modules`` is checked before ``sys.meta_path``. The loader must
    refuse the helper at scan time, naming the file and the collision.
    """
    import sys

    # _json is a CPython built-in module, always in sys.modules.
    assert "_json" in sys.modules
    (local_dir / "_json.py").write_text("VALUE = 'helper'\n")
    (local_dir / "acme.py").write_text(
        BODY.format(
            cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
        ).replace(
            "class AcmeHarness(Harness):",
            "import _json\nclass AcmeHarness(Harness):",
        )
    )
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["acme"]["error"] is not None
    error = rows["acme"]["error"]
    assert "_json.py" in error
    assert "already loaded" in error
    assert "rename" in error


def test_non_python_files_are_ignored(local_dir):
    (local_dir / "notes.txt").write_text("not a plugin")
    (local_dir / "acme.py.bak").write_text("also not")
    plugin(local_dir)
    assert "acme" in install(local_dir)


def test_plugins_load_in_filename_order(local_dir):
    plugin(local_dir, "b.py", cls="B", name="bee", binary="bee")
    plugin(local_dir, "a.py", cls="A", name="ay", binary="ay")
    loaded = plugins.scan(local_dir, source=plugins.LOCAL)
    assert [p.name for p in loaded] == ["ay", "bee"]


def test_a_plugin_does_not_take_a_real_module_name(local_dir):
    """A user's `json.py` must not become *the* json module for the daemon."""
    import json
    import sys

    plugin(local_dir, "json.py", cls="JsonHarness", name="jsonish", binary="j")
    install(local_dir)
    assert sys.modules["json"] is json
    assert f"{plugins.MODULE_PREFIX}local_json" in sys.modules


def test_the_two_sources_do_not_share_module_names(local_dir, shipped_dir):
    """A local `vibe.py` must not evict the module the shipped one came from."""
    import sys

    plugin(shipped_dir, "vibe.py", cls="TheirVibe", name="vibe", binary="vibe")
    plugin(local_dir, "vibe.py", cls="MyVibe", name="vibe", binary="vibe")
    install(local_dir, shipped_dir=shipped_dir)
    assert f"{plugins.MODULE_PREFIX}shipped_vibe" in sys.modules
    assert f"{plugins.MODULE_PREFIX}local_vibe" in sys.modules


def test_installing_twice_is_the_same_as_once(local_dir):
    plugin(local_dir)
    install(local_dir)
    assert install(local_dir) == ["acme", "claude", "codex", "opencode", "vibe"]


def test_installing_an_empty_directory_drops_the_plugin(local_dir, tmp_path):
    plugin(local_dir)
    install(local_dir)
    install(tmp_path / "gone")
    assert "acme" not in harness_registry.HARNESSES


# ---- refusals -----------------------------------------------------------


def test_a_plugin_that_raises_on_import_names_the_file(local_dir):
    (local_dir / "broken.py").write_text("raise ValueError('boom')")
    assert "broken.py" in error_in(local_dir)


def test_a_plugin_with_a_syntax_error_names_the_file(local_dir):
    (local_dir / "broken.py").write_text("def (:")
    assert "broken.py" in error_in(local_dir)


def test_a_plugin_that_fails_to_import_leaves_no_module_behind(local_dir):
    """A half-executed module in sys.modules would be found by the next import."""
    import sys

    (local_dir / "broken.py").write_text("raise ValueError('boom')")
    install(local_dir)
    assert f"{plugins.MODULE_PREFIX}local_broken" not in sys.modules


def test_a_plugin_with_no_harness_says_what_to_write(local_dir):
    (local_dir / "empty.py").write_text("x = 1\n")
    assert "HARNESS = MyHarness" in error_in(local_dir)


def test_exporting_the_class_instead_of_an_instance_is_caught(local_dir):
    """The likeliest mistake, and the error tells you the fix verbatim."""
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
    ).replace("HARNESS = AcmeHarness()", "HARNESS = AcmeHarness")
    (local_dir / "acme.py").write_text(body)
    assert "AcmeHarness()" in error_in(local_dir)


def test_a_harness_that_is_not_a_harness_is_caught(local_dir):
    (local_dir / "odd.py").write_text("HARNESS = 'a string'\n")
    assert "does not subclass" in error_in(local_dir)


def test_a_harness_with_no_observer_is_caught(local_dir):
    """The one attribute Python cannot enforce for us.

    `Harness.observer` is an annotation, not an abstract property, so a plugin
    that forgets it instantiates happily and fails much later inside the
    daemon's watch loop. The loader is where that has to be caught, and the
    error has to say what to write.
    """
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
    ).replace("        self.observer = AcmeHarnessObserver()", "        pass")
    (local_dir / "acme.py").write_text(body)
    error = error_in(local_dir)
    assert "sets no observer" in error
    assert "self.observer = MyObserver()" in error


def test_an_observer_that_is_not_an_observer_is_caught(local_dir):
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
    ).replace("        self.observer = AcmeHarnessObserver()", '        self.observer = "nope"')
    (local_dir / "acme.py").write_text(body)
    assert "does not subclass" in error_in(local_dir)


def test_exporting_the_observer_class_instead_of_an_instance_is_caught(local_dir):
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
    ).replace(
        "        self.observer = AcmeHarnessObserver()",
        "        self.observer = AcmeHarnessObserver",
    )
    (local_dir / "acme.py").write_text(body)
    assert "not an instance" in error_in(local_dir)


def test_an_illegal_name_is_caught(local_dir):
    plugin(local_dir, name="My Acme")
    assert "lowercase letters" in error_in(local_dir)


def test_a_missing_binary_is_caught(local_dir):
    plugin(local_dir, binary="")
    assert "no binary" in error_in(local_dir)


def test_a_multi_character_icon_is_caught(local_dir):
    """Two columns wide would shift every row of every listing."""
    plugin(local_dir, icon="<>")
    assert "display width" in error_in(local_dir)


def test_a_wide_emoji_icon_is_caught(local_dir):
    """A single-codepoint wide emoji occupies two cells and shears every column."""
    plugin(local_dir, icon="🚀")
    assert "display width" in error_in(local_dir)


def test_a_control_character_icon_is_caught(local_dir):
    """A control character is not printable, so it must not pass."""
    plugin(local_dir, icon="\n")
    assert "printable" in error_in(local_dir)


def test_an_empty_icon_is_caught(local_dir):
    plugin(local_dir, icon="")
    assert "printable" in error_in(local_dir)


def test_a_plain_ascii_icon_is_accepted(local_dir):
    plugin(local_dir, icon="@")
    assert "acme" in install(local_dir)


def test_a_single_cell_non_ascii_icon_is_accepted(local_dir):
    plugin(local_dir, icon="\u25c7")
    assert "acme" in install(local_dir)


def test_a_variation_selector_icon_is_caught(local_dir):
    """U+FE0F is category Mn (zero-width) but combining class 0, so the old
    combining-class test let it through.  It totals zero cells and must be
    rejected by the width branch."""
    plugin(local_dir, icon="\ufe0f")
    assert "display width" in error_in(local_dir)


def test_a_combining_grapheme_joiner_icon_is_caught(local_dir):
    """U+034F is category Mn (zero-width) but combining class 0.  Same hole."""
    plugin(local_dir, icon="\u034f")
    assert "display width" in error_in(local_dir)


def test_a_base_plus_combining_icon_is_accepted(local_dir):
    """A base character plus a combining acute is one display cell."""
    plugin(local_dir, icon="e\u0301")
    assert "acme" in install(local_dir)


def test_shipped_icons_still_pass():
    """All four shipped icons are category So — the Mn/Me predicate must not
    touch them.  This is the regression guard for the algorithm change."""
    harness_registry.install(cfg.Config())
    for name in ("claude", "codex", "opencode", "vibe"):
        harness = harness_registry.get(name)
        assert harness.icon, f"{name} has no icon"
        # The loader's own check must accept it.
        plugins._check_identity(harness_registry._PLUGINS[name].path, harness)


def test_two_plugins_with_one_name_name_both_files(local_dir):
    plugin(local_dir, "one.py", cls="One")
    plugin(local_dir, "two.py", cls="Two")
    with pytest.raises(harness_registry.ConfigError) as exc:
        install(local_dir)
    assert "one.py" in str(exc.value) and "two.py" in str(exc.value)


# ---- the two sources fail differently -----------------------------------


def test_a_broken_local_plugin_does_not_stop_start_up(local_dir):
    """The user wrote it and can see it; one bad file is not worth the daemon."""
    (local_dir / "broken.py").write_text("raise ValueError('boom')")
    plugin(local_dir)
    assert install(local_dir) == ["acme", "claude", "codex", "opencode", "vibe"]


def test_a_broken_local_plugin_is_listed_as_broken(local_dir):
    (local_dir / "broken.py").write_text("raise ValueError('boom')")
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["broken"]["source"] == "local"
    assert "boom" in rows["broken"]["error"]
    assert rows["broken"]["installed"] is False


def test_a_broken_shipped_plugin_is_fatal(local_dir, shipped_dir):
    """An adapter we ship that will not load is our bug, not a warning."""
    (shipped_dir / "acme.py").write_text("raise ValueError('boom')")
    with pytest.raises(PluginError, match=r"acme\.py"):
        install(local_dir, shipped_dir=shipped_dir)


def test_a_broken_shipped_plugin_names_the_escape_hatch(local_dir, shipped_dir):
    (shipped_dir / "acme.py").write_text("raise ValueError('boom')")
    with pytest.raises(PluginError) as exc:
        install(local_dir, shipped_dir=shipped_dir)
    assert 'disabled = ["acme"]' in str(exc.value)


def test_a_local_plugin_that_calls_sys_exit_does_not_stop_start_up(local_dir):
    """C2: ``sys.exit()`` at import raises ``SystemExit`` (a ``BaseException``),
    not ``Exception`` — without catching it, the daemon dies."""
    (local_dir / "quitter.py").write_text("import sys; sys.exit(1)")
    plugin(local_dir)
    assert install(local_dir) == ["acme", "claude", "codex", "opencode", "vibe"]


def test_a_local_plugin_that_calls_sys_exit_is_listed_as_broken(local_dir):
    (local_dir / "quitter.py").write_text("import sys; sys.exit(1)")
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["quitter"]["source"] == "local"
    assert rows["quitter"]["installed"] is False


def test_a_local_plugin_with_none_aliases_does_not_stop_start_up(local_dir):
    """C2: ``aliases = None`` is not iterable and would raise ``TypeError``
    outside any handler."""
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
    ).replace("aliases = ()", "aliases = None")
    (local_dir / "bad.py").write_text(body)
    assert install(local_dir) == ["claude", "codex", "opencode", "vibe"]


def test_a_local_plugin_with_none_aliases_is_listed_as_broken(local_dir):
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
    ).replace("aliases = ()", "aliases = None")
    (local_dir / "bad.py").write_text(body)
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["bad"]["source"] == "local"
    assert "not iterable" in rows["bad"]["error"]


def test_a_local_plugin_with_string_aliases_is_listed_as_broken(local_dir):
    """C2: ``aliases = "nova"`` is the classic iterable-of-characters trap."""
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
    ).replace("aliases = ()", 'aliases = "nova"')
    (local_dir / "bad.py").write_text(body)
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert "iterable of characters" in rows["bad"]["error"]


def test_a_local_plugin_with_list_binaries_is_accepted_and_normalised(local_dir):
    """C2: ``binaries = ["nova"]`` (list, not frozenset) is accepted and
    normalised to ``frozenset`` — the loader does not reject spellings that
    work fine."""
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
    ).replace(
        "class AcmeHarness(Harness):",
        "class AcmeHarness(Harness):\n    binaries = ['nova']",
    )
    (local_dir / "acme.py").write_text(body)
    assert "acme" in install(local_dir)
    assert harness_registry.get("acme").binaries == frozenset({"nova"})


def test_a_local_plugin_with_list_aliases_is_accepted_and_normalised(local_dir):
    """C2: ``aliases = ["nova-cli"]`` (list, not tuple) is accepted and
    normalised to ``tuple``."""
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
    ).replace("aliases = ()", 'aliases = ["nova-cli"]')
    (local_dir / "acme.py").write_text(body)
    assert "acme" in install(local_dir)
    assert harness_registry.get("acme").aliases == ("nova-cli",)


def test_a_local_plugin_with_string_binaries_is_listed_as_broken(local_dir):
    """C2: ``binaries = "nova"`` is the iterable-of-characters trap."""
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon=repr("@"), aliases="()"
    ).replace(
        "class AcmeHarness(Harness):",
        'class AcmeHarness(Harness):\n    binaries = "nova"',
    )
    (local_dir / "bad.py").write_text(body)
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert "iterable of characters" in rows["bad"]["error"]


def test_a_local_plugin_with_empty_binary_element_is_listed_as_broken(local_dir):
    """R5-5: ``binaries = frozenset({None, ""})`` is accepted by
    ``frozenset()`` but the elements are invalid."""
    (local_dir / "bad.py").write_text(
        "from theater.harness import Harness, LaunchPlan, TranscriptObserver\n"
        "class BadObserver(TranscriptObserver):\n"
        "    def find_transcript(self, *, cwd, session_id=None, after=None): return None\n"
        "    def session_id(self, transcript): return None\n"
        "    def parse(self, line, index, *, clip_text=True): return []\n"
        "    def is_idle_screen(self, capture): return capture.endswith('> ')\n"
        "class BadHarness(Harness):\n"
        "    name = 'acme'; binary = 'acme'; icon = '@'\n"
        "    binaries = frozenset({None, ''})\n"
        "    def __init__(self): self.observer = BadObserver()\n"
        "    def plan_launch(self, *, participant_id, prompt, config_path, approval):\n"
        "        return LaunchPlan(argv=[self.binary, prompt], env={'ID': participant_id})\n"
        "HARNESS = BadHarness()\n"
    )
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["bad"]["source"] == "local"
    assert "empty binary name" in rows["bad"]["error"]


def test_a_local_plugin_with_unhashable_binary_element_gets_correct_diagnosis(local_dir):
    """R5-5: ``binaries = [[]]`` is iterable but ``frozenset([[]])`` fails
    with ``TypeError: unhashable type``. The error must say "not hashable",
    not "not iterable" — a list IS iterable."""
    (local_dir / "bad.py").write_text(
        "from theater.harness import Harness, LaunchPlan, TranscriptObserver\n"
        "class BadObserver(TranscriptObserver):\n"
        "    def find_transcript(self, *, cwd, session_id=None, after=None): return None\n"
        "    def session_id(self, transcript): return None\n"
        "    def parse(self, line, index, *, clip_text=True): return []\n"
        "    def is_idle_screen(self, capture): return capture.endswith('> ')\n"
        "class BadHarness(Harness):\n"
        "    name = 'acme'; binary = 'acme'; icon = '@'\n"
        "    binaries = [[]]\n"
        "    def __init__(self): self.observer = BadObserver()\n"
        "    def plan_launch(self, *, participant_id, prompt, config_path, approval):\n"
        "        return LaunchPlan(argv=[self.binary, prompt], env={'ID': participant_id})\n"
        "HARNESS = BadHarness()\n"
    )
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["bad"]["source"] == "local"
    assert "not hashable" in rows["bad"]["error"]
    assert "not iterable" not in rows["bad"]["error"]


def test_a_local_plugin_with_a_runtime_error_raising_iterable_is_broken(local_dir):
    """R5-3: an iterable that raises ``RuntimeError`` while being consumed
    must become a broken plugin, not escape ``scan`` and kill the daemon."""
    (local_dir / "bad.py").write_text(
        "from theater.harness import Harness, LaunchPlan, TranscriptObserver\n"
        "class _BoomIter:\n"
        "    def __iter__(self): return self\n"
        "    def __next__(self): raise RuntimeError('boom during consumption')\n"
        "class BadObserver(TranscriptObserver):\n"
        "    def find_transcript(self, *, cwd, session_id=None, after=None): return None\n"
        "    def session_id(self, transcript): return None\n"
        "    def parse(self, line, index, *, clip_text=True): return []\n"
        "    def is_idle_screen(self, capture): return capture.endswith('> ')\n"
        "class BadHarness(Harness):\n"
        "    name = 'acme'; binary = 'acme'; icon = '@'\n"
        "    aliases = _BoomIter()\n"
        "    def __init__(self): self.observer = BadObserver()\n"
        "    def plan_launch(self, *, participant_id, prompt, config_path, approval):\n"
        "        return LaunchPlan(argv=[self.binary, prompt], env={'ID': participant_id})\n"
        "HARNESS = BadHarness()\n"
    )
    assert install(local_dir) == ["claude", "codex", "opencode", "vibe"]
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["bad"]["source"] == "local"
    assert "could not be consumed" in rows["bad"]["error"]


def test_a_local_plugin_with_a_read_only_aliases_property_is_broken(local_dir):
    """R5-3: a harness whose ``aliases`` setter raises ``AttributeError``
    must become a broken plugin, not escape ``scan``."""
    (local_dir / "bad.py").write_text(
        "from theater.harness import Harness, LaunchPlan, TranscriptObserver\n"
        "class BadObserver(TranscriptObserver):\n"
        "    def find_transcript(self, *, cwd, session_id=None, after=None): return None\n"
        "    def session_id(self, transcript): return None\n"
        "    def parse(self, line, index, *, clip_text=True): return []\n"
        "    def is_idle_screen(self, capture): return capture.endswith('> ')\n"
        "class BadHarness(Harness):\n"
        "    name = 'acme'; binary = 'acme'; icon = '@'\n"
        "    @property\n"
        "    def aliases(self): return ()\n"
        "    @aliases.setter\n"
        "    def aliases(self, value): raise AttributeError('read-only')\n"
        "    def __init__(self): self.observer = BadObserver()\n"
        "    def plan_launch(self, *, participant_id, prompt, config_path, approval):\n"
        "        return LaunchPlan(argv=[self.binary, prompt], env={'ID': participant_id})\n"
        "HARNESS = BadHarness()\n"
    )
    assert install(local_dir) == ["claude", "codex", "opencode", "vibe"]
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["bad"]["source"] == "local"
    assert "could not be set" in rows["bad"]["error"]


def test_disabling_a_plugin_stops_it_being_imported(local_dir, shipped_dir):
    """The escape hatch has to work when importing is exactly what breaks."""
    (shipped_dir / "acme.py").write_text("raise ValueError('boom')")
    plugin(shipped_dir, "vibe.py", cls="TheirVibe", name="vibe", binary="vibe")
    assert install(local_dir, disabling("acme"), shipped_dir=shipped_dir) == ["vibe"]


# ---- precedence ---------------------------------------------------------


def test_a_local_plugin_overrides_a_shipped_one(local_dir):
    """Whoever wrote their own vibe.py has said which one they want."""
    plugin(local_dir, "vibe.py", cls="MyVibe", name="vibe", binary="vibe")
    install(local_dir)
    assert type(harness_registry.get("vibe")).__name__ == "MyVibe"
    # The shipped aliases still resolve, and still to the same name.
    assert harness_registry.normalize("mistral-vibe") == "vibe"


def test_an_override_reports_itself_as_local(local_dir):
    plugin(local_dir, "vibe.py", cls="MyVibe", name="vibe", binary="vibe")
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["vibe"]["source"] == "local"


def test_a_plugin_alias_normalizes(local_dir):
    plugin(local_dir, aliases=("acme-cli",))
    install(local_dir)
    assert harness_registry.normalize("acme-cli") == "acme"


def test_a_plugin_alias_cannot_shadow_another_harness(local_dir):
    plugin(local_dir, aliases=("mistral-vibe",))
    with pytest.raises(harness_registry.ConfigError, match="already resolves"):
        install(local_dir)


def test_a_plugin_alias_cannot_be_another_harness_name(local_dir):
    plugin(local_dir, aliases=("claude",))
    with pytest.raises(harness_registry.ConfigError, match="name of another"):
        install(local_dir)


def test_a_plugin_name_cannot_shadow_an_existing_alias(local_dir, shipped_dir):
    """C1: a primary name colliding with an already-claimed alias is refused.

    Alias registered first, then the colliding name: ``a.py`` claims
    ``mistral-vibe`` as an alias of ``vibe``, then ``b.py`` tries to register
    under the primary name ``mistral-vibe``. Without the guard, the name lands
    in ``HARNESSES`` but ``normalize("mistral-vibe")`` still returns ``"vibe"``
    — registration and adoption route to the wrong adapter.
    """
    plugin(shipped_dir, "a.py", cls="Vibe2", name="vibe", binary="vibe", aliases=("mistral-vibe",))
    plugin(local_dir, "b.py", cls="Mv", name="mistral-vibe", binary="mv")
    with pytest.raises(harness_registry.ConfigError, match="already an alias of"):
        install(local_dir, shipped_dir=shipped_dir)


def test_a_plugin_alias_cannot_shadow_an_existing_name(local_dir, shipped_dir):
    """C1 mirror: name registered first, then the colliding alias.

    The reverse interleaving: ``a.py`` registers as ``mistral-vibe``, then
    ``b.py`` claims ``mistral-vibe`` as an alias of ``vibe``. The existing
    ``_claim_alias`` guard catches this, and the test pins both orderings to
    the same outcome.
    """
    plugin(shipped_dir, "a.py", cls="Mv", name="mistral-vibe", binary="mv")
    plugin(local_dir, "b.py", cls="Vibe2", name="vibe", binary="vibe", aliases=("mistral-vibe",))
    with pytest.raises(harness_registry.ConfigError, match="name of another"):
        install(local_dir, shipped_dir=shipped_dir)


def test_two_plugins_cannot_claim_the_same_binary(local_dir, shipped_dir):
    """C3: two adapters claiming the same binary are silently resolved by
    iteration order in ``match_binary``. Refused at load time with both
    files named, the same shape as the alias collision guard."""
    plugin(shipped_dir, "a.py", cls="First", name="first", binary="nova")
    plugin(local_dir, "b.py", cls="Second", name="second", binary="nova")
    with pytest.raises(harness_registry.ConfigError, match="already claimed by"):
        install(local_dir, shipped_dir=shipped_dir)


def test_two_plugins_cannot_claim_the_same_extra_binary(local_dir, shipped_dir):
    """C3: a ``binaries`` entry colliding with another harness's primary
    ``binary`` is also refused."""
    plugin(shipped_dir, "a.py", cls="First", name="first", binary="nova")
    (local_dir / "b.py").write_text(
        BODY.format(
            cls="Second", name="second", binary="other", icon=repr("#"), aliases="()"
        ).replace(
            "class Second(Harness):",
            "class Second(Harness):\n    binaries = frozenset({'nova'})",
        )
    )
    with pytest.raises(harness_registry.ConfigError, match="already claimed by"):
        install(local_dir, shipped_dir=shipped_dir)


def test_wrapped_binary_collides_with_unwrapped_primary(local_dir, shipped_dir):
    """R5-2: ``_claim_binary`` must normalise the same way ``match_binary``
    does. ``binary = "foo"`` and ``binaries = frozenset({".foo-wrapped"})``
    are treated as distinct by a raw-string comparison but ``match_binary``
    unwraps ``.foo-wrapped`` to ``foo`` and matches both. The guard must
    catch this at load time."""
    plugin(shipped_dir, "a.py", cls="Aaa", name="aaa", binary="foo")
    (local_dir / "b.py").write_text(
        BODY.format(cls="Bbb", name="bbb", binary="bar", icon=repr("#"), aliases="()").replace(
            "class Bbb(Harness):",
            "class Bbb(Harness):\n    binaries = frozenset({'.foo-wrapped'})",
        )
    )
    with pytest.raises(harness_registry.ConfigError, match="already claimed by"):
        install(local_dir, shipped_dir=shipped_dir)


def test_path_shaped_binary_collides_with_basename(local_dir, shipped_dir):
    """R5-2: ``match_binary`` tests ``command in names``, so a path-shaped
    binary declaration collides with the bare basename. The guard must catch
    this too."""
    plugin(shipped_dir, "a.py", cls="Aaa", name="aaa", binary="/opt/foo")
    plugin(local_dir, "b.py", cls="Bbb", name="bbb", binary="foo")
    with pytest.raises(harness_registry.ConfigError, match="already claimed by"):
        install(local_dir, shipped_dir=shipped_dir)


def test_a_local_override_releases_the_shipped_binary_claims(local_dir, shipped_dir):
    """R5-6: when a local plugin overrides a shipped one of the same name,
    the shipped harness's binary and alias claims must be released. Otherwise
    a binary the override drops stays claimed by the shipped harness, and a
    later plugin claiming it is refused for no reason a user can see."""
    # Shipped harness "acme" claims binary "foo" and alias "foo-cli".
    plugin(
        shipped_dir, "acme.py", cls="ShippedAcme", name="acme", binary="foo", aliases=("foo-cli",)
    )
    # Local override "acme" claims binary "bar" instead, dropping "foo" and "foo-cli".
    plugin(local_dir, "acme.py", cls="LocalAcme", name="acme", binary="bar")
    # A third plugin claiming "foo" should succeed — the shipped claim was released.
    plugin(local_dir, "zzz.py", cls="Zzz", name="zzz", binary="foo")
    result = install(local_dir, shipped_dir=shipped_dir)
    assert "acme" in result
    assert "zzz" in result
    assert harness_registry.get("acme").binary == "bar"
    assert harness_registry.get("zzz").binary == "foo"
    # The alias "foo-cli" still resolves to "acme" because the override has the
    # same name — aliases resolve to a harness name, not a file.
    assert harness_registry.normalize("foo-cli") == "acme"


def test_binary_collision_error_names_both_files(local_dir, shipped_dir):
    """R5-7: the binary collision error must name both the claimant's file
    and the previous owner's file, not just the previous harness name."""
    plugin(shipped_dir, "a.py", cls="First", name="first", binary="nova")
    plugin(local_dir, "b.py", cls="Second", name="second", binary="nova")
    with pytest.raises(harness_registry.ConfigError) as exc:
        install(local_dir, shipped_dir=shipped_dir)
    msg = str(exc.value)
    assert "b.py" in msg
    assert "a.py" in msg


# ---- observation key collisions (tmux 15-char truncation) -----------------


def test_two_long_binaries_sharing_first_15_chars_are_refused(local_dir, shipped_dir):
    """Two harnesses whose binary names are longer than 15 characters but
    share their first 15 characters would appear identical in tmux's
    ``pane_current_command``.  Refused at load time — preferring one would
    silently misidentify the other."""
    # "very-long-binary" (17 chars) and "very-long-binary2" (18 chars) both
    # truncate to "very-long-binary" (15 chars) in tmux.
    plugin(shipped_dir, "a.py", cls="First", name="first", binary="very-long-binary")
    plugin(local_dir, "b.py", cls="Second", name="second", binary="very-long-binary2")
    with pytest.raises(harness_registry.ConfigError) as exc:
        install(local_dir, shipped_dir=shipped_dir)
    msg = str(exc.value)
    assert "observation key" in msg
    assert "a.py" in msg
    assert "b.py" in msg
    assert "very-long-binary" in msg  # the ambiguous 15-char key


def test_exact_15_char_binary_collides_with_truncated_form(local_dir, shipped_dir):
    """A harness whose binary is exactly 15 characters collides with another
    harness whose longer binary truncates to the same 15 characters.  Both
    would appear identical in tmux — refused, not 'prefer exact'."""
    plugin(shipped_dir, "a.py", cls="First", name="first", binary="exactly15chars")
    # "exactly15chars2" (16 chars) truncates to "exactly15chars" (15 chars)
    plugin(local_dir, "b.py", cls="Second", name="second", binary="exactly15chars2")
    with pytest.raises(harness_registry.ConfigError) as exc:
        install(local_dir, shipped_dir=shipped_dir)
    msg = str(exc.value)
    assert "observation key" in msg
    assert "a.py" in msg
    assert "b.py" in msg
    assert "exactly15chars" in msg


def test_two_truncated_spellings_of_same_harness_are_accepted(local_dir, shipped_dir):
    """Multiple spellings claimed by the SAME harness are not a collision —
    the same harness's primary binary and ``binaries`` entry may both produce
    the same truncated observation key."""
    plugin(shipped_dir, "a.py", cls="Solo", name="solo", binary="very-long-binary")
    # The same harness also declares an extra binary that truncates to the
    # same 15 chars — same owner, so not a collision.
    (local_dir / "a.py").write_text(
        BODY.format(
            cls="Solo", name="solo", binary="very-long-binary", icon=repr("@"), aliases="()"
        ).replace(
            "class Solo(Harness):",
            "class Solo(Harness):\n    binaries = frozenset({'very-long-binary2'})",
        )
    )
    result = install(local_dir, shipped_dir=shipped_dir)
    assert "solo" in result


# ---- disabling ----------------------------------------------------------


def test_a_disabled_harness_is_absent(local_dir):
    assert install(local_dir, disabling("vibe")) == ["claude", "codex", "opencode"]
    assert "vibe" not in harness_registry.HARNESSES


def test_a_disabled_harness_leaves_the_unmanaged_sweep(local_dir):
    """Nothing should be looking for a binary Theater has been told to ignore."""
    install(local_dir, disabling("vibe"))
    assert "vibe" not in harness_registry.known_binaries()


def test_a_disabled_harness_is_not_offered_by_the_palette(local_dir):
    from theater.regie.palette import entries

    install(local_dir, disabling("vibe"))
    offered = [name for _, name, _ in entries(harness_registry.describe())]
    assert offered == ["claude", "codex", "opencode"]


def test_a_disabled_harness_still_draws_in_the_tree(local_dir):
    """An agent that exists must be visible even if Theater cannot read it."""
    install(local_dir, disabling("vibe"))
    assert harness_registry.harness_icon("vibe") == harness_registry.UNKNOWN_ICON


def test_disabling_something_that_is_not_there_is_not_an_error(local_dir):
    """Names come and go across releases; a stale entry is not worth a crash."""
    assert install(local_dir, disabling("nosuchharness")) == [
        "claude",
        "codex",
        "opencode",
        "vibe",
    ]


# ---- the rest of the system sees them -----------------------------------


def test_the_cli_loads_plugins_from_theater_home(capsys):
    """`ensure_home` makes the directory; the CLI reads it before dispatching."""
    paths.ensure_home()
    plugin(paths.harnesses_dir())
    assert cli.main(["harnesses"]) == 0
    out = capsys.readouterr().out
    assert "acme" in out


def test_a_broken_plugin_is_reported_by_the_cli(capsys):
    paths.ensure_home()
    (paths.harnesses_dir() / "broken.py").write_text("raise ValueError('boom')")
    assert cli.main(["harnesses"]) == 0
    out = capsys.readouterr().out
    assert "broken" in out and "boom" in out


def test_a_plugin_binary_joins_the_unmanaged_sweep(local_dir):
    plugin(local_dir, binary="acme-bin")
    install(local_dir)
    assert "acme-bin" in harness_registry.known_binaries()


def test_a_plugin_shows_up_in_describe(local_dir):
    plugin(local_dir)
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["acme"]["icon"] == "@"
    assert rows["acme"]["binary"] == "acme"
    assert rows["acme"]["source"] == "local"
    assert rows["acme"]["error"] is None


def test_a_plugin_is_observable_from_its_transcript(local_dir):
    """`has_transcript` decides which watch loop the daemon runs.

    Read off the observer, not the harness: the daemon is handed only that half
    and never asks a harness how to watch it.
    """
    plugin(local_dir)
    install(local_dir)
    observer = harness_registry.get("acme").observer
    assert observer.has_transcript is True
    assert observer.is_idle_screen("something\n> ") is True


# ---- F1: scan() never raises, even for non-PluginError exceptions -------------


def test_scan_rejects_plugin_with_raising_property_getter(local_dir):
    """F1: a plugin whose property getter raises RuntimeError is rejected,
    not fatal to the daemon.

    Mutation: revert scan() to ``except (PluginError, SystemExit)``. This
    test fails because RuntimeError escapes scan() instead of producing a
    rejected Plugin.
    """
    bad = local_dir / "evil.py"
    bad.write_text(
        """
from theater.harness import Harness, LaunchPlan, TranscriptObserver


class EvilObserver(TranscriptObserver):
    def find_transcript(self, *, cwd, session_id=None, after=None):
        return None
    def session_id(self, transcript):
        return None
    def parse(self, line, index, *, clip_text=True):
        return []
    def is_idle_screen(self, capture):
        return capture.endswith("> ")


class Evil(Harness):
    binary = "evil"
    icon = "E"
    aliases = ()
    def __init__(self):
        self.observer = EvilObserver()
    @property
    def name(self):
        raise RuntimeError("a property getter that raises something else")
    def plan_launch(self, *, participant_id, prompt, config_path, approval):
        return LaunchPlan(argv=[self.binary, prompt], env={"ID": participant_id})


HARNESS = Evil()
""",
        encoding="utf-8",
    )
    # A valid plugin in the same directory — it must still load
    plugin(local_dir, name="acme", binary="acme")

    found = plugins.scan(local_dir, source=plugins.LOCAL)
    by_name = {p.name: p for p in found}

    # The broken plugin is rejected, not fatal
    assert "evil" in by_name
    assert by_name["evil"].harness is None
    assert by_name["evil"].error is not None
    assert "evil.py" in by_name["evil"].error
    assert "RuntimeError" in by_name["evil"].error

    # The valid plugin still loaded
    assert by_name["acme"].harness is not None
    assert by_name["acme"].error is None


# ---- F5: helper import does not swallow KeyboardInterrupt ---------------------


def test_helper_import_does_not_swallow_keyboard_interrupt(local_dir):
    """F5: a KeyboardInterrupt during helper import propagates out of scan(),
    it is not reported as a broken plugin.

    Mutation: revert the helper except to ``except BaseException``. This
    test fails because KeyboardInterrupt is caught and re-raised as
    PluginError, so scan() does NOT raise and the plugin is listed as
    rejected instead.
    """
    # A helper that raises KeyboardInterrupt when imported
    helper = local_dir / "_helper.py"
    helper.write_text(
        "raise KeyboardInterrupt('ctrl-C during helper import')\n",
        encoding="utf-8",
    )
    main = local_dir / "needs_helper.py"
    main.write_text(
        "import _helper  # noqa: F401\n"
        "from theater.harness import Harness, LaunchPlan, TranscriptObserver\n\n"
        "class O(TranscriptObserver):\n"
        "    def find_transcript(self, *, cwd, session_id=None, after=None):\n"
        "        return None\n"
        "    def session_id(self, transcript):\n"
        "        return None\n"
        "    def parse(self, line, index, *, clip_text=True):\n"
        "        return []\n"
        "    def is_idle_screen(self, capture):\n"
        "        return False\n\n"
        "class H(Harness):\n"
        "    name = 'h'\n"
        "    binary = 'h'\n"
        "    icon = 'H'\n"
        "    aliases = ()\n"
        "    def __init__(self):\n"
        "        self.observer = O()\n"
        "    def plan_launch(self, *, participant_id, prompt, config_path, approval):\n"
        "        return LaunchPlan(argv=[self.binary, prompt])\n\n"
        "HARNESS = H()\n",
        encoding="utf-8",
    )

    # With the fix, KeyboardInterrupt propagates out of scan() — it is not
    # caught and reported as a broken plugin.
    with pytest.raises(KeyboardInterrupt):
        plugins.scan(local_dir, source=plugins.LOCAL)
