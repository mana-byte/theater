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

from theater.harness import HARNESSES
from theater.regie import palette
from theater.regie.app import RegieApp
from theater.regie.palette import SpawnCommands, entries


class FakeApp:
    def __init__(self) -> None:
        self.spawned: list[str] = []

    def spawn_harness(self, harness: str) -> None:
        self.spawned.append(harness)


class FakeScreen:
    def __init__(self, app: FakeApp) -> None:
        self.app = app


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(palette.shutil, "which", lambda binary: f"/usr/bin/{binary}")


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
    monkeypatch.setattr(palette.shutil, "which", lambda binary: None)
    rows = entries()
    assert {name for _, name, _ in rows} == set(HARNESSES)
    assert all("not on PATH" in help_text for _, _, help_text in rows)


def test_an_installed_binary_says_what_the_entry_will_do(installed):
    assert all("no prompt" in help_text for _, _, help_text in entries())


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


# ---- app wiring ---------------------------------------------------------


def test_the_provider_is_registered_without_dropping_the_built_ins():
    assert SpawnCommands in RegieApp.COMMANDS
    assert RegieApp.COMMANDS > {SpawnCommands}


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
