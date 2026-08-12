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

from theater import cli
from theater import config as cfg
from theater import harness as harness_registry
from theater import paths
from theater.harness import plugins
from theater.harness.plugins import PluginError

#: A plugin that satisfies both ABCs with the least code that can work. Every
#: test that is not about a specific method starts from this. Note the shape it
#: forces: a harness that launches, and a separate observer it carries, which
#: is the whole point of the v1.6 split.
BODY = '''
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
    icon = "{icon}"
    aliases = {aliases}

    def __init__(self):
        self.observer = {cls}Observer()

    def plan_launch(self, *, participant_id, prompt, config_path, approval):
        return LaunchPlan(argv=[self.binary, prompt], env={{"ID": participant_id}})


HARNESS = {cls}()
'''


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
        BODY.format(
            cls=cls, name=name, binary=binary, icon=icon, aliases=repr(aliases)
        ),
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


def test_a_plugin_is_a_full_adapter(local_dir):
    plugin(local_dir)
    install(local_dir)
    assert harness_registry.get("acme").observer.has_transcript is True


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


def test_an_empty_directory_leaves_the_shipped_set(local_dir):
    assert install(local_dir) == ["claude", "codex", "opencode", "vibe"]


def test_underscored_files_are_skipped(local_dir):
    """So a plugin can keep a helper module beside it."""
    (local_dir / "_shared.py").write_text("raise RuntimeError('imported')")
    plugin(local_dir)
    assert "acme" in install(local_dir)


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
        cls="AcmeHarness", name="acme", binary="acme", icon="@", aliases="()"
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
        cls="AcmeHarness", name="acme", binary="acme", icon="@", aliases="()"
    ).replace("        self.observer = AcmeHarnessObserver()", "        pass")
    (local_dir / "acme.py").write_text(body)
    error = error_in(local_dir)
    assert "sets no observer" in error
    assert "self.observer = MyObserver()" in error


def test_an_observer_that_is_not_an_observer_is_caught(local_dir):
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon="@", aliases="()"
    ).replace("        self.observer = AcmeHarnessObserver()", '        self.observer = "nope"')
    (local_dir / "acme.py").write_text(body)
    assert "does not subclass" in error_in(local_dir)


def test_exporting_the_observer_class_instead_of_an_instance_is_caught(local_dir):
    body = BODY.format(
        cls="AcmeHarness", name="acme", binary="acme", icon="@", aliases="()"
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
    assert "one character" in error_in(local_dir)


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
    with pytest.raises(PluginError, match="acme.py"):
        install(local_dir, shipped_dir=shipped_dir)


def test_a_broken_shipped_plugin_names_the_escape_hatch(local_dir, shipped_dir):
    (shipped_dir / "acme.py").write_text("raise ValueError('boom')")
    with pytest.raises(PluginError) as exc:
        install(local_dir, shipped_dir=shipped_dir)
    assert 'disabled = ["acme"]' in str(exc.value)


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


def test_ensure_home_creates_the_directory():
    paths.ensure_home()
    assert paths.harnesses_dir().is_dir()


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
