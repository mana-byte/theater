"""The `Spawn <harness>` entries in the régie's command palette.

The provider is exercised directly against a stand-in screen rather than
through a running app: `Provider` only ever reaches for `screen.app`, and
booting the real régie would connect to a daemon and start spawning windows.

What can actually be wrong here is the wiring — every hit calling the same
harness because a closure captured the loop variable, or an entry vanishing
because the binary is not installed on this particular machine.
"""

from __future__ import annotations

import pytest

from theater import harness as harness_registry
from theater.config import Config, TheaterSection
from theater.harness import HARNESSES
from theater.regie.app import RegieApp
from theater.regie.palette import SpawnCommands, ViewCommands, entries


class FakeApp:
    def __init__(self, settings=None) -> None:
        self.spawned: list[str] = []
        # Only set when a test cares. The unset case is the one the provider
        # has to survive: Textual builds providers against whatever app is
        # running, which in a screen test is not a RegieApp.
        if settings is not None:
            self.settings = settings

    def spawn_harness(self, harness: str) -> None:
        self.spawned.append(harness)


class FakeScreen:
    def __init__(self, app: FakeApp) -> None:
        self.app = app


@pytest.fixture
def installed(monkeypatch):
    """Pretend every harness binary is on PATH.

    Patched in `theater.harness`, which is where install state is now decided:
    the palette no longer looks at PATH itself, it renders whatever rows the
    daemon handed it.
    """
    monkeypatch.setattr(harness_registry.shutil, "which", lambda binary: f"/usr/bin/{binary}")


@pytest.fixture
def provider(installed):
    app = FakeApp()
    return SpawnCommands(FakeScreen(app)), app


# ---- entries ------------------------------------------------------------


def test_every_registered_harness_is_offered(installed):
    assert {name for _, name, _ in entries()} == set(HARNESSES)


def test_an_entry_shows_the_harness_icon(installed):
    display = {name: text for text, name, _ in entries()}
    for name, harness in HARNESSES.items():
        assert harness.icon in display[name]
        assert name in display[name]


def test_a_missing_binary_is_listed_and_labelled(monkeypatch):
    """Dropping the entry would read as 'Theater cannot drive this harness'."""
    monkeypatch.setattr(harness_registry.shutil, "which", lambda binary: None)
    rows = entries()
    assert {name for _, name, _ in rows} == set(HARNESSES)
    assert all("not on PATH" in help_text for _, _, help_text in rows)


def test_an_installed_binary_says_what_the_entry_will_do(installed):
    assert all("no prompt" in help_text for _, _, help_text in entries())


# ---- favourite ordering -------------------------------------------------


def test_without_a_favourite_the_order_is_alphabetical(installed):
    assert [name for _, name, _ in entries()] == sorted(HARNESSES)


def test_the_favourite_is_offered_first(installed):
    last = sorted(HARNESSES)[-1]
    assert next(name for _, name, _ in entries(favourite=last)) == last


def test_promoting_a_favourite_drops_nobody(installed):
    """A favourite ranks the list; it does not filter it."""
    last = sorted(HARNESSES)[-1]
    assert {name for _, name, _ in entries(favourite=last)} == set(HARNESSES)


def test_the_rest_stay_alphabetical_behind_the_favourite(installed):
    last = sorted(HARNESSES)[-1]
    names = [name for _, name, _ in entries(favourite=last)]
    assert names[1:] == sorted(set(HARNESSES) - {last})


def test_a_favourite_that_is_not_a_harness_is_ignored(installed):
    """Config can name anything; the palette is not where that is policed."""
    assert [name for _, name, _ in entries(favourite="nope")] == sorted(HARNESSES)


# ---- the rows come from the caller --------------------------------------


def test_supplied_rows_are_used_instead_of_the_registry():
    """A harness the daemon declared from config is not importable here."""
    rows = [
        {"name": "codex", "icon": "◇", "binary": "codex", "installed": True},
        {"name": "opencode", "icon": "◈", "binary": "opencode", "installed": False},
    ]
    assert [name for _, name, _ in entries(rows)] == ["codex", "opencode"]


def test_supplied_rows_keep_their_order_behind_the_favourite():
    """The daemon sorted them; re-sorting here would fight it."""
    rows = [
        {"name": "codex", "icon": "◇", "binary": "codex", "installed": True},
        {"name": "opencode", "icon": "◈", "binary": "opencode", "installed": True},
        {"name": "vibe", "icon": "▲", "binary": "vibe", "installed": True},
    ]
    names = [name for _, name, _ in entries(rows, favourite="vibe")]
    assert names == ["vibe", "codex", "opencode"]


def test_a_supplied_row_reports_its_own_install_state(monkeypatch):
    """PATH is the daemon's to resolve — it is the process that spawns."""
    monkeypatch.setattr(harness_registry.shutil, "which", lambda binary: "/usr/bin/x")
    rows = [{"name": "codex", "icon": "◇", "binary": "codex", "installed": False}]
    assert "not on PATH" in entries(rows)[0][2]


# ---- provider -----------------------------------------------------------


async def test_the_entries_appear_before_anything_is_typed(provider):
    """Discovery hits are the point: the user should not have to guess a word."""
    prov, _ = provider
    hits = [hit async for hit in prov.discover()]
    assert len(hits) == len(HARNESSES)


async def test_each_hit_spawns_its_own_harness(provider):
    """A closure over the loop variable would make every hit spawn the last one."""
    prov, app = provider
    async for hit in prov.discover():
        hit.command()
    assert sorted(app.spawned) == sorted(HARNESSES)


async def test_searching_a_harness_name_finds_only_it(provider):
    prov, app = provider
    hits = [hit async for hit in prov.search("vibe")]
    assert len(hits) == 1
    hits[0].command()
    assert app.spawned == ["vibe"]


async def test_a_query_matching_nothing_yields_nothing(provider):
    prov, _ = provider
    assert [hit async for hit in prov.search("zzzzz")] == []


async def test_hits_are_scored_so_the_palette_can_rank_them(provider):
    prov, _ = provider
    hits = [hit async for hit in prov.search("spawn")]
    assert hits and all(hit.score > 0 for hit in hits)


async def test_the_daemons_list_is_what_gets_offered(installed):
    """Not the local registry: the daemon is the process that will refuse."""
    rows = [{"name": "codex", "icon": "◇", "binary": "codex", "installed": True}]
    app = FakeApp()
    app.harnesses = rows
    prov = SpawnCommands(FakeScreen(app))
    hits = [hit async for hit in prov.discover()]
    assert len(hits) == 1
    hits[0].command()
    assert app.spawned == ["codex"]


async def test_a_failed_harness_call_falls_back_to_the_registry(provider):
    """`harnesses` is None when the call failed; an empty palette would lie."""
    prov, app = provider
    app.harnesses = None
    assert len([hit async for hit in prov.discover()]) == len(HARNESSES)


async def test_an_app_without_settings_still_gets_a_palette(provider):
    """The provider reads config off the app defensively, and must not assume."""
    prov, _app = provider
    assert len([hit async for hit in prov.discover()]) == len(HARNESSES)


async def test_the_configured_favourite_is_discovered_first(installed):
    last = sorted(HARNESSES)[-1]
    settings = Config(theater=TheaterSection(favourite=last))
    prov = SpawnCommands(FakeScreen(FakeApp(settings)))
    first = await anext(prov.discover())
    first.command()
    assert prov.app.spawned == [last]


# ---- app wiring ---------------------------------------------------------


def test_the_provider_is_registered_without_dropping_the_built_ins():
    assert SpawnCommands in RegieApp.COMMANDS
    assert {SpawnCommands} < RegieApp.COMMANDS


# ---- what the app actually asks the daemon for --------------------------


class FakeClient:
    """Records the daemon calls a spawn makes, and answers the refresh after."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "spawn":
            return {"id": "abc123", "harness": params["harness"], "tmux_pane": "%9"}
        return []


@pytest.fixture
def spawning_app(monkeypatch):
    app = RegieApp()
    client = FakeClient()
    app._client = client
    app.my_session_name = "main"
    monkeypatch.setattr(app, "notify", lambda *a, **k: None)
    return app, client


def _spawn_params(client: FakeClient) -> dict:
    return next(params for method, params in client.calls if method == "spawn")


async def test_a_palette_spawn_is_unparented(spawning_app):
    """The palette starts a CLI; it does not delegate work to a child."""
    app, client = spawning_app
    await app._spawn_harness("vibe")
    assert _spawn_params(client).get("parent_id") is None


async def test_a_palette_spawn_carries_no_prompt(spawning_app):
    app, client = spawning_app
    await app._spawn_harness("vibe")
    params = _spawn_params(client)
    assert params["prompt"] == ""
    assert params["harness"] == "vibe"
    assert params["approval"] == "manual"


async def test_a_palette_spawn_lands_in_the_session_the_user_is_looking_at(
    spawning_app,
):
    """By name: the spawner matches `list-sessions`, which does not print $ids."""
    app, client = spawning_app
    await app._spawn_harness("vibe")
    assert _spawn_params(client)["tmux_session"] == "main"


async def test_the_tree_is_refreshed_so_the_new_agent_shows_up(spawning_app):
    app, client = spawning_app
    await app._spawn_harness("vibe")
    assert any(method == "participants.tree" for method, _ in client.calls)


async def test_a_refused_spawn_does_not_take_the_app_down(monkeypatch):
    """An uninstalled harness is a BadRequest, not a crash."""
    app = RegieApp()
    reported: list[str] = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: reported.append(msg))

    class Refusing:
        async def call(self, method: str, **params):
            raise RuntimeError("'claude' is not on PATH")

    app._client = Refusing()
    await app._spawn_harness("claude")
    assert reported and "not on PATH" in reported[0]


# ---- the app asks the daemon for the list -------------------------------


async def test_the_app_loads_the_harness_list_at_mount():
    app = RegieApp()
    client = FakeClient()
    app._client = client
    await app._load_harnesses()
    assert any(method == "harnesses" for method, _ in client.calls)


async def test_the_loaded_list_is_what_the_palette_offers():
    rows = [{"name": "codex", "icon": "◇", "binary": "codex", "installed": True}]

    class Answering:
        async def call(self, method, **params):
            return rows

    app = RegieApp()
    app._client = Answering()
    await app._load_harnesses()
    prov = SpawnCommands(FakeScreen(app))
    assert [name for _, name, _ in prov._entries()] == ["codex"]


async def test_a_failed_load_leaves_the_registry_fallback_in_place():
    """An empty palette would say 'nothing can be spawned', which is false."""

    class Refusing:
        async def call(self, method, **params):
            raise RuntimeError("daemon went away")

    app = RegieApp()
    app._client = Refusing()
    await app._load_harnesses()
    assert app.harnesses is None


async def test_loading_without_a_client_is_not_an_error():
    """on_mount runs before the connection on a daemonless start."""
    app = RegieApp()
    app._client = None
    await app._load_harnesses()
    assert app.harnesses is None


# ---- view commands -------------------------------------------------------


class TogglingApp:
    """The two attributes ViewCommands reads, and nothing else."""

    def __init__(self, bus_visible: bool = True) -> None:
        self.bus_visible = bus_visible
        self.toggled = 0

    def action_toggle_bus(self) -> None:
        self.toggled += 1


async def test_a_showing_bus_panel_is_offered_the_hide():
    prov = ViewCommands(FakeScreen(TogglingApp(bus_visible=True)))
    hit = await anext(prov.discover())
    assert hit.display == "Hide bus panel"


async def test_a_hidden_bus_panel_is_offered_the_show():
    """The entry names the action, so the user is not guessing at the state."""
    prov = ViewCommands(FakeScreen(TogglingApp(bus_visible=False)))
    hit = await anext(prov.discover())
    assert hit.display == "Show bus panel"


async def test_running_the_hit_toggles_the_panel():
    app = TogglingApp()
    prov = ViewCommands(FakeScreen(app))
    hit = await anext(prov.discover())
    hit.command()
    assert app.toggled == 1


async def test_the_toggle_is_searchable_by_name():
    prov = ViewCommands(FakeScreen(TogglingApp()))
    assert [hit.command async for hit in prov.search("bus")]
    assert [hit async for hit in prov.search("zzzzz")] == []


async def test_an_app_that_cannot_toggle_offers_nothing():
    """Textual builds providers against whatever app is running."""
    prov = ViewCommands(FakeScreen(FakeApp()))
    assert [hit async for hit in prov.discover()] == []
    assert [hit async for hit in prov.search("bus")] == []


async def test_the_view_provider_is_registered_on_the_app():
    assert ViewCommands in RegieApp.COMMANDS
