from __future__ import annotations

from regie.palette import (
    ResumeSessionCommand,
    ResumeSessionCommands,
    SpawnChoice,
    SpawnCommand,
    SpawnHarnessCommands,
    ViewCommands,
    spawn_approval,
)
from regie.resume import ResumeCandidate


class _App:
    def __init__(self) -> None:
        self.spawned: list[str] = []
        self.spawned_in_directory: list[str] = []
        self.resumed: list[ResumeCandidate] = []
        self.opened: list[str] = []
        self.bus_visible = False

    def _spawn_choices(self) -> tuple[SpawnChoice, ...]:
        return (
            SpawnChoice("codex", True, approvals=("manual", "edits", "yolo")),
            SpawnChoice("vibe", False, "vibe is unavailable"),
        )

    def action_spawn(self) -> None:
        self.opened.append("spawn")

    def spawn_harness(self, harness: str) -> None:
        self.spawned.append(harness)

    def spawn_harness_in_directory(self, harness: str) -> None:
        self.spawned_in_directory.append(harness)

    def action_resume_palette(self) -> None:
        self.opened.append("resume")

    def resume_dead_session(self, candidate: ResumeCandidate) -> None:
        self.resumed.append(candidate)

    def action_toggle_bus(self) -> None:
        self.bus_visible = not self.bus_visible


class _Screen:
    def __init__(self, app: _App) -> None:
        self.app = app


async def test_spawn_provider_fuzzy_searches_public_choices_and_binds_each_harness() -> None:
    app = _App()
    provider = SpawnHarnessCommands(_Screen(app))  # type: ignore[arg-type]

    discovered = [hit async for hit in provider.discover()]
    assert [str(hit.display) for hit in discovered] == [
        "◉ Spawn codex",
        "◉ Spawn codex in directory…",
        "▤ Spawn vibe",
    ]
    assert discovered[2].help == "vibe is unavailable"

    matches = [hit async for hit in provider.search("cdx")]
    assert len(matches) == 2
    matches[0].command()
    assert app.spawned == ["codex"]

    [directory_match] = [hit async for hit in provider.search("directory")]
    directory_match.command()
    assert app.spawned_in_directory == ["codex"]


def test_spawn_policy_prefers_manual_or_the_only_advertised_policy() -> None:
    assert spawn_approval(SpawnChoice("codex", True, approvals=("yolo", "manual"))) == "manual"
    assert spawn_approval(SpawnChoice("pi", True, approvals=("yolo",))) == "yolo"
    assert spawn_approval(SpawnChoice("future", True, approvals=("edits", "yolo"))) is None


async def test_resume_provider_searches_description_path_and_stable_id() -> None:
    app = _App()
    provider = ResumeSessionCommands(_Screen(app))  # type: ignore[arg-type]
    candidate = ResumeCandidate(
        "participant-dead-1",
        "codex",
        "/workspace/theater",
        "session-1",
        True,
        name="retired worker",
        description="repair the schema migration",
    )
    provider._candidates = (candidate,)

    for query in ("schema", "workspace", "dead-1"):
        [match] = [hit async for hit in provider.search(query)]
        match.command()

    assert app.resumed == [candidate, candidate, candidate]


async def test_root_palette_keeps_rc9_spawn_resume_and_bus_commands() -> None:
    app = _App()
    screen = _Screen(app)
    providers = (SpawnCommand(screen), ResumeSessionCommand(screen), ViewCommands(screen))  # type: ignore[arg-type]

    hits = [[hit async for hit in provider.discover()] for provider in providers]
    assert [str(group[0].display) for group in hits] == [
        "Spawn",
        "Resume sessions",
        "Show bus panel",
    ]
    for group in hits:
        group[0].command()

    assert app.opened == ["spawn", "resume"]
    assert app.bus_visible
