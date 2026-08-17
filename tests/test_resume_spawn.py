"""Tests for the resume chain: MCP tool -> daemon -> spawner -> plugin.

``resume`` mirrors ``model`` exactly: an optional capability validated up
front, forwarded only when non-None, refused before anything is created.
These tests cover the full path from the spawn RPC down to ``plan_launch``
receiving the session id, the up-front refusals, and the B2 trap where a
harness that silently drops the prompt on resume must not be handed both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theater.daemon.spawner import Spawner, SpawnRequest
from theater.harness import HARNESSES, Harness, LaunchPlan
from theater.harness.builtin.plugins.vibe import ISOLATION_MARKER, isolation_marker_text
from theater.models import BadRequest


def _trusted_resume(
    registry,
    *,
    harness: str,
    session_id: str = "sess-abc",
    live: bool = False,
):
    p = registry.register(harness=harness, pane=None, cwd="/tmp", session_id=session_id)
    p.session_correlation = "exact"
    registry.store.upsert_participant(p)
    if not live:
        registry.mark_dead(p.id)
    return p


class _ResumeHarness(Harness):
    """A harness that accepts resume and delivers the prompt on the command line."""

    name = "resume-spawn-test"
    binary = "resume-spawn-test"
    icon = "R"

    def __init__(self):
        from theater.harness.observation import TranscriptObserver

        class _Obs(TranscriptObserver):
            has_transcript = True

            def find_transcript(self, *, cwd, session_id=None, after=None):
                return None

            def session_id(self, transcript):
                return None

            def parse(self, line, index, *, clip_text=True):
                return []

            def is_idle_screen(self, capture):
                return False

        self.observer = _Obs()

    seen_resume: str | None = None

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        resume: str | None = None,
    ) -> LaunchPlan:
        self.seen_resume = resume
        argv = ["resume-spawn-test"]
        if resume:
            argv += ["--resume", resume]
        if prompt:
            argv += ["--prompt", prompt]
        return LaunchPlan(argv=argv)


class _NoResumeHarness(Harness):
    """A harness whose plan_launch predates the resume parameter."""

    name = "no-resume-spawn-test"
    binary = "no-resume-spawn-test"
    icon = "N"

    def __init__(self):
        from theater.harness.observation import TranscriptObserver

        class _Obs(TranscriptObserver):
            has_transcript = True

            def find_transcript(self, *, cwd, session_id=None, after=None):
                return None

            def session_id(self, transcript):
                return None

            def parse(self, line, index, *, clip_text=True):
                return []

            def is_idle_screen(self, capture):
                return False

        self.observer = _Obs()

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
    ) -> LaunchPlan:
        return LaunchPlan(argv=["no-resume-spawn-test", participant_id])


class _DropsPromptHarness(_ResumeHarness):
    """A harness that accepts resume but silently drops the prompt (opencode)."""

    name = "drops-prompt-test"
    binary = "drops-prompt-test"
    icon = "D"
    resume_takes_prompt = False


@pytest.fixture
def resume_harness(monkeypatch):
    h = _ResumeHarness()
    monkeypatch.setitem(HARNESSES, "resume-spawn-test", h)
    return h


@pytest.fixture
def no_resume_harness(monkeypatch):
    h = _NoResumeHarness()
    monkeypatch.setitem(HARNESSES, "no-resume-spawn-test", h)
    return h


@pytest.fixture
def drops_prompt_harness(monkeypatch):
    h = _DropsPromptHarness()
    monkeypatch.setitem(HARNESSES, "drops-prompt-test", h)
    return h


# ---- resume reaches plan_launch ---------------------------------------


async def test_resume_reaches_plan_launch(registry, resume_harness, monkeypatch):
    """The session id travels from SpawnRequest through plan_launch."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_resume(registry, harness="resume-spawn-test")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    await spawner.spawn(req)
    assert resume_harness.seen_resume == "sess-abc"


async def test_resume_none_does_not_reach_plan_launch(registry, resume_harness, monkeypatch):
    """A None resume is not forwarded, identical to the model contract."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
    )
    await spawner.spawn(req)
    assert resume_harness.seen_resume is None


# ---- check_resume refuses unsupported harnesses ----------------------


async def test_check_resume_refuses_unsupported_harness(registry, no_resume_harness, monkeypatch):
    """A resume asked of a harness without the parameter is refused up front."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="no-resume-spawn-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="does not support resume"):
        await spawner.spawn(req)


# ---- B2: a harness that drops the prompt on resume --------------------


async def test_resume_with_prompt_refused_for_dropping_harness(
    registry, drops_prompt_harness, monkeypatch
):
    """opencode accepts -s but drops --prompt; both must not be passed.

    Option (b): refuse with BadRequest, naming the harness and pointing at
    the alternative (resume without a prompt, then send).  Refused before
    the participant or worktree exists, so nothing is left behind.
    """
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="drops-prompt-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="cannot resume a session with a prompt"):
        await spawner.spawn(req)


async def test_resume_without_prompt_allowed_for_dropping_harness(
    registry, drops_prompt_harness, monkeypatch
):
    """Resuming without a prompt is fine even for a harness that drops prompts.

    The trap is specifically both resume AND prompt; resume alone has no
    prompt to drop.
    """
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_resume(registry, harness="drops-prompt-test")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="drops-prompt-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    await spawner.spawn(req)
    assert drops_prompt_harness.seen_resume == "sess-abc"


async def test_resume_refuses_unknown_session_id(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )

    with pytest.raises(BadRequest, match="no trusted"):
        await spawner.spawn(req)


async def test_resume_refuses_heuristic_session_id(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    p = registry.register(
        harness="resume-spawn-test",
        pane=None,
        cwd="/tmp",
        session_id="sess-abc",
    )
    p.session_correlation = "heuristic"
    registry.store.upsert_participant(p)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )

    with pytest.raises(BadRequest, match="no trusted"):
        await spawner.spawn(req)


async def test_resume_allows_trusted_dead_session(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_resume(registry, harness="resume-spawn-test")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )

    await spawner.spawn(req)

    assert resume_harness.seen_resume == "sess-abc"


async def test_resume_refuses_live_trusted_session_id(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    p = _trusted_resume(registry, harness="resume-spawn-test", live=True)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )

    with pytest.raises(BadRequest, match=f"trusted owner {p.id} is still live"):
        await spawner.spawn(req)


async def test_dead_trusted_binding_remains_resumable_when_transcript_is_missing(
    registry, resume_harness, monkeypatch, tmp_path, fake_tmux
):
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    p = _trusted_resume(registry, harness="resume-spawn-test")
    p = registry.get(p.id)
    p.transcript_location = str(tmp_path / "missing" / "messages.jsonl")
    p.transcript_domain = str(tmp_path.resolve())
    registry.store.upsert_participant(p)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )

    spawned = await spawner.spawn(req)

    assert spawned.status.value == "idle"
    assert resume_harness.seen_resume == "sess-abc"


async def test_vibe_resume_reuses_trusted_isolated_domain(registry, tmp_path, fake_tmux):
    project = tmp_path / "project"
    project.mkdir()
    domain = tmp_path / "isolated-vibe"
    domain.mkdir()
    transcript = domain / "session_20260817_120000_sessabc1" / "messages.jsonl"
    transcript.parent.mkdir()
    transcript.write_text('{"role":"assistant","content":"old"}\n', encoding="utf-8")
    predecessor = registry.register(
        harness="vibe",
        pane=None,
        cwd=str(project),
        session_id="sessabc1-1111-2222-3333",
    )
    (domain / ISOLATION_MARKER).write_text(
        isolation_marker_text(participant_id=predecessor.id, transcript_domain=domain),
        encoding="utf-8",
    )
    predecessor.session_correlation = "operator"
    predecessor.transcript_domain = str(domain.resolve())
    predecessor.transcript_location = str(transcript)
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    spawned = await Spawner(registry).spawn(
        SpawnRequest(
            harness="vibe",
            prompt="continue",
            cwd=str(project),
            approval="manual",
            resume="sessabc1-1111-2222-3333",
        )
    )

    assert fake_tmux.windows[-1]["env"]["VIBE_SESSION_LOGGING__SAVE_DIR"] == str(domain.resolve())
    assert registry.get(spawned.id).transcript_domain == str(domain.resolve())


async def test_vibe_resume_can_repeat_from_successor(registry, tmp_path, fake_tmux):
    project = tmp_path / "project"
    project.mkdir()
    spawner = Spawner(registry)

    cold = await spawner.spawn(
        SpawnRequest(
            harness="vibe",
            prompt="start",
            cwd=str(project),
            approval="manual",
        )
    )
    cold = registry.get(cold.id)
    assert cold.transcript_domain is not None
    domain = Path(cold.transcript_domain)
    transcript = domain / "session_20260817_120000_sessabc1" / "messages.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"role":"assistant","content":"cold"}\n', encoding="utf-8")
    cold.session_id = "sessabc1-1111-2222-3333"
    cold.session_correlation = "exact"
    cold.transcript_location = str(transcript)
    registry.store.upsert_participant(cold)
    registry.mark_dead(cold.id)

    first = await spawner.spawn(
        SpawnRequest(
            harness="vibe",
            prompt="resume once",
            cwd=str(project),
            approval="manual",
            resume="sessabc1-1111-2222-3333",
        )
    )
    registry.mark_dead(first.id)

    second = await spawner.spawn(
        SpawnRequest(
            harness="vibe",
            prompt="resume twice",
            cwd=str(project),
            approval="manual",
            resume="sessabc1-1111-2222-3333",
        )
    )

    assert fake_tmux.windows[-2]["env"]["VIBE_SESSION_LOGGING__SAVE_DIR"] == str(domain)
    assert fake_tmux.windows[-1]["env"]["VIBE_SESSION_LOGGING__SAVE_DIR"] == str(domain)
    assert registry.get(first.id).transcript_domain == str(domain)
    assert registry.get(second.id).transcript_domain == str(domain)


async def test_vibe_resume_refuses_live_successor_even_with_dead_lineage(
    registry, tmp_path, fake_tmux
):
    project = tmp_path / "project"
    project.mkdir()
    spawner = Spawner(registry)

    cold = await spawner.spawn(
        SpawnRequest(
            harness="vibe",
            prompt="start",
            cwd=str(project),
            approval="manual",
        )
    )
    cold = registry.get(cold.id)
    assert cold.transcript_domain is not None
    domain = Path(cold.transcript_domain)
    transcript = domain / "session_20260817_120000_sessabc1" / "messages.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"role":"assistant","content":"cold"}\n', encoding="utf-8")
    cold.session_id = "sessabc1-1111-2222-3333"
    cold.session_correlation = "exact"
    cold.transcript_location = str(transcript)
    registry.store.upsert_participant(cold)
    registry.mark_dead(cold.id)

    live = await spawner.spawn(
        SpawnRequest(
            harness="vibe",
            prompt="resume once",
            cwd=str(project),
            approval="manual",
            resume="sessabc1-1111-2222-3333",
        )
    )

    with pytest.raises(BadRequest, match=f"trusted owner {live.id} is still live"):
        await spawner.spawn(
            SpawnRequest(
                harness="vibe",
                prompt="resume twice",
                cwd=str(project),
                approval="manual",
                resume="sessabc1-1111-2222-3333",
            )
        )

    assert len(fake_tmux.windows) == 2


async def test_vibe_resume_refuses_unrelated_trusted_row_for_marked_domain(
    registry, tmp_path, fake_tmux
):
    project = tmp_path / "project"
    project.mkdir()
    domain = tmp_path / "isolated-vibe"
    domain.mkdir()
    owner = registry.register(
        harness="vibe",
        pane=None,
        cwd=str(project),
        session_id="owner-session-1111",
    )
    (domain / ISOLATION_MARKER).write_text(
        isolation_marker_text(participant_id=owner.id, transcript_domain=domain),
        encoding="utf-8",
    )
    owner.session_correlation = "exact"
    owner.transcript_domain = str(domain.resolve())
    registry.store.upsert_participant(owner)
    unrelated = registry.register(
        harness="vibe",
        pane=None,
        cwd=str(project),
        session_id="resume-session-2222",
    )
    unrelated.session_correlation = "exact"
    unrelated.transcript_domain = str(domain.resolve())
    registry.store.upsert_participant(unrelated)
    registry.mark_dead(unrelated.id)

    with pytest.raises(BadRequest, match="different Theater session lineage"):
        await Spawner(registry).spawn(
            SpawnRequest(
                harness="vibe",
                prompt="continue",
                cwd=str(project),
                approval="manual",
                resume="resume-session-2222",
            )
        )

    assert fake_tmux.windows == []


async def test_vibe_resume_refuses_legacy_shared_root(registry, tmp_path, fake_tmux):
    project = tmp_path / "project"
    project.mkdir()
    shared = tmp_path / "shared-vibe"
    shared.mkdir()
    predecessor = registry.register(
        harness="vibe",
        pane=None,
        cwd=str(project),
        session_id="sessabc1-1111-2222-3333",
    )
    predecessor.session_correlation = "proven"
    predecessor.transcript_domain = str(shared.resolve())
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    with pytest.raises(BadRequest, match="Rebind or migrate"):
        await Spawner(registry).spawn(
            SpawnRequest(
                harness="vibe",
                prompt="continue",
                cwd=str(project),
                approval="manual",
                resume="sessabc1-1111-2222-3333",
            )
        )

    assert fake_tmux.windows == []


async def test_resume_with_response_format_refused_before_side_effects(
    registry, drops_prompt_harness, monkeypatch
):
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="drops-prompt-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
        response_format='{"type":"json_schema"}',
    )
    with pytest.raises(BadRequest, match="cannot resume a session with response_format"):
        await spawner.spawn(req)
    assert registry.list(include_dead=True) == []


# ---- resume + worktree is refused ------------------------------------


async def test_resume_with_worktree_refused(registry, resume_harness, monkeypatch):
    """A resumed session's transcript describes files at its original cwd;
    a fresh worktree points it at different files."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
        worktree=True,
    )
    with pytest.raises(BadRequest, match="cannot resume into a worktree"):
        await spawner.spawn(req)


async def test_resume_with_named_worktree_refused(registry, resume_harness, monkeypatch):
    """A named worktree is also refused with resume, for the same reason."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
        worktree="shared-name",
    )
    with pytest.raises(BadRequest, match="cannot resume into a worktree"):
        await spawner.spawn(req)


# ---- MCP server surface: resume in the schema -------------------------


async def test_spawn_session_schema_includes_resume(daemon):
    """The MCP tool schema exposes resume as an optional parameter."""
    from theater.mcp.server import build

    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    props = schema["spawn_session"]["properties"]
    assert "resume" in props
    assert "resume" not in schema["spawn_session"].get("required", [])


# ---- resume floor persistence ---------------------------------------------


async def test_resume_persists_floor_on_successor(registry, resume_harness, monkeypatch, tmp_path):
    """A resume spawn captures the predecessor's stream floor on the successor."""
    from theater.resume_floor import UNKNOWN_FLOOR

    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    transcript_path = tmp_path / "messages.jsonl"
    transcript_path.write_text('{"role":"assistant","content":"old"}\n', encoding="utf-8")
    predecessor = _trusted_resume(registry, harness="resume-spawn-test")
    predecessor = registry.get(predecessor.id)
    predecessor.transcript_location = str(transcript_path)
    registry.store.upsert_participant(predecessor)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    spawned = await spawner.spawn(req)
    reloaded = registry.store.get_participant(spawned.id)
    assert reloaded.resume_floor is not None
    assert reloaded.resume_floor != UNKNOWN_FLOOR


async def test_resume_floor_unknown_when_transcript_missing(
    registry, resume_harness, monkeypatch, tmp_path
):
    """A missing predecessor transcript location produces an unknown floor."""
    from theater.resume_floor import UNKNOWN_FLOOR

    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    predecessor = _trusted_resume(registry, harness="resume-spawn-test")
    predecessor = registry.get(predecessor.id)
    # transcript_location stays None — the predecessor never attached
    registry.store.upsert_participant(predecessor)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    spawned = await spawner.spawn(req)
    reloaded = registry.store.get_participant(spawned.id)
    assert reloaded.resume_floor == UNKNOWN_FLOOR


async def test_cold_spawn_has_no_floor(registry, resume_harness, monkeypatch):
    """A cold spawn (no resume) has a NULL resume_floor."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
    )
    spawned = await spawner.spawn(req)
    reloaded = registry.store.get_participant(spawned.id)
    assert reloaded.resume_floor is None


async def test_resume_floor_unknown_when_file_unreadable(
    registry, resume_harness, monkeypatch, tmp_path
):
    """An unreadable predecessor transcript produces an unknown floor."""
    from theater.resume_floor import UNKNOWN_FLOOR

    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    transcript_path = tmp_path / "missing" / "messages.jsonl"
    predecessor = _trusted_resume(registry, harness="resume-spawn-test")
    predecessor = registry.get(predecessor.id)
    predecessor.transcript_location = str(transcript_path)
    registry.store.upsert_participant(predecessor)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    spawned = await spawner.spawn(req)
    reloaded = registry.store.get_participant(spawned.id)
    assert reloaded.resume_floor == UNKNOWN_FLOOR
