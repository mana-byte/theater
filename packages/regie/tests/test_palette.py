from __future__ import annotations

from regie.controllers.actions import ActionRecord, ActionState
from regie.palette import (
    ResumeSessionCommand,
    ResumeSessionCommands,
    RetryActionCommand,
    SpawnChoice,
    SpawnCommand,
    SpawnHarnessCommands,
    TranscriptCandidateCommands,
    ViewCommands,
    spawn_approval,
)
from regie.resume import ResumeCandidate

from theater.frontend import TranscriptCandidate


class _App:
    def __init__(self) -> None:
        self.spawned: list[str] = []
        self.resumed: list[ResumeCandidate] = []
        self.opened: list[str] = []
        self.bus_visible = False
        self.uncertain: ActionRecord | None = None
        self.retried = 0
        self._transcript_recovery_target: str | None = None
        self.transcript_selections: list[tuple[TranscriptCandidate, str | None]] = []

    def _spawn_choices(self) -> tuple[SpawnChoice, ...]:
        return (
            SpawnChoice("codex", True, approvals=("manual", "edits", "yolo")),
            SpawnChoice("vibe", False, "vibe is unavailable"),
        )

    def action_spawn(self) -> None:
        self.opened.append("spawn")

    def spawn_harness(self, harness: str) -> None:
        self.spawned.append(harness)

    def action_resume_palette(self) -> None:
        self.opened.append("resume")

    def resume_dead_session(self, candidate: ResumeCandidate) -> None:
        self.resumed.append(candidate)

    def action_toggle_bus(self) -> None:
        self.bus_visible = not self.bus_visible

    def latest_uncertain_action(self) -> ActionRecord | None:
        return self.uncertain

    def retry_latest_action(self) -> None:
        self.retried += 1

    def select_transcript_candidate(
        self,
        candidate: TranscriptCandidate,
        *,
        participant_id: str | None = None,
    ) -> None:
        self.transcript_selections.append((candidate, participant_id))


class _Screen:
    def __init__(self, app: _App) -> None:
        self.app = app


async def test_spawn_provider_fuzzy_searches_public_choices_and_binds_each_harness() -> None:
    app = _App()
    provider = SpawnHarnessCommands(_Screen(app))  # type: ignore[arg-type]

    discovered = [hit async for hit in provider.discover()]
    assert [str(hit.display) for hit in discovered] == [
        "Spawn codex",
        "Spawn vibe",
    ]
    assert discovered[1].help == "vibe is unavailable"
    for hit in discovered:
        hit.command()
    assert app.spawned == ["codex", "vibe"]

    [match] = [hit async for hit in provider.search("cdx")]
    match.command()
    assert app.spawned == ["codex", "vibe", "codex"]


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


async def test_resume_provider_uses_the_public_catalog_icon() -> None:
    app = _App()
    app.icon_for_harness = lambda _harness: "◈"  # type: ignore[attr-defined]
    provider = ResumeSessionCommands(_Screen(app))  # type: ignore[arg-type]
    provider._candidates = (
        ResumeCandidate(
            "participant-dead-1",
            "custom",
            "/workspace",
            "session-1",
            True,
        ),
    )

    [entry] = [hit async for hit in provider.discover()]

    assert str(entry.display).startswith("◈ custom ")


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


async def test_retry_palette_entry_exists_only_for_an_uncertain_action() -> None:
    app = _App()
    provider = RetryActionCommand(_Screen(app))  # type: ignore[arg-type]
    assert [hit async for hit in provider.discover()] == []

    app.uncertain = ActionRecord(
        "send",
        "participant-a",
        "key-a",
        state=ActionState.UNCERTAIN,
    )
    [hit] = [hit async for hit in provider.discover()]
    assert str(hit.display) == "Retry uncertain send"
    hit.command()
    assert app.retried == 1


def test_transcript_candidate_command_captures_the_original_participant() -> None:
    app = _App()
    app._transcript_recovery_target = "participant-original"
    provider = TranscriptCandidateCommands(_Screen(app))  # type: ignore[arg-type]
    candidate = TranscriptCandidate(
        "/tmp/transcript.jsonl",
        session_id="session-a",
        provenance="exact",
    )
    command = provider._command(candidate)

    app._transcript_recovery_target = "participant-later"
    command()

    assert app.transcript_selections == [(candidate, "participant-original")]
