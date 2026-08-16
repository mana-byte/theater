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
from theater.models import BadRequest


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


# ---- MCP server surface: resume in the schema -------------------------


async def test_spawn_session_schema_includes_resume(daemon):
    """The MCP tool schema exposes resume as an optional parameter."""
    from theater.mcp.server import build

    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    props = schema["spawn_session"]["properties"]
    assert "resume" in props
    assert "resume" not in schema["spawn_session"].get("required", [])
