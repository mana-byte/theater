"""Resume capability checks and forwarding from the spawn RPC down to plan_launch."""

from __future__ import annotations

import pytest

from tests.support.resume_harnesses import (
    DropsPromptHarness,
    NoResumeHarness,
    ResumeHarness,
    trusted_resume,
)
from theater.constants.daemon import BUS_KIND_PARTICIPANT_SESSION_BOUNDARY
from theater.daemon.spawner import Spawner, SpawnRequest
from theater.harness import (
    HARNESSES,
    check_resume,
    plan_launch,
    supports_model,
    supports_reasoning,
    supports_resume,
)
from theater.harness.contracts.harness import LaunchParameterSupport
from theater.models import BadRequest


@pytest.fixture
def resume_harness(monkeypatch):
    h = ResumeHarness()
    monkeypatch.setitem(HARNESSES, "resume-spawn-test", h)
    return h


@pytest.fixture
def no_resume_harness(monkeypatch):
    h = NoResumeHarness()
    monkeypatch.setitem(HARNESSES, "no-resume-spawn-test", h)
    return h


@pytest.fixture
def drops_prompt_harness(monkeypatch):
    h = DropsPromptHarness()
    monkeypatch.setitem(HARNESSES, "drops-prompt-test", h)
    return h


async def test_resume_reaches_plan_launch(registry, resume_harness, monkeypatch):
    """The session id travels from SpawnRequest through plan_launch."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    predecessor = trusted_resume(registry, harness="resume-spawn-test")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    successor = (await spawner.reserve(req)).participant
    assert resume_harness.seen_resume == "sess-abc"
    assert successor.resumed_from_id == predecessor.id


async def test_resume_inherits_description_but_not_name(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    predecessor = trusted_resume(registry, harness="resume-spawn-test")
    predecessor = registry.get(predecessor.id)
    predecessor.description = "Preserve this purpose"
    registry.store.upsert_participant(predecessor)
    spawner = Spawner(registry)

    reservation = await spawner.reserve(
        SpawnRequest(
            harness="resume-spawn-test",
            prompt="do thing",
            cwd="/tmp",
            approval="edits",
            resume="sess-abc",
            name="New-Live-Name",
        )
    )
    successor = reservation.participant

    assert successor.description == "Preserve this purpose"
    assert successor.name == "New-Live-Name"


async def test_resume_empty_description_clears_inherited_value(
    registry, resume_harness, monkeypatch
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    predecessor = trusted_resume(registry, harness="resume-spawn-test")
    predecessor = registry.get(predecessor.id)
    predecessor.description = "Clear this purpose"
    registry.store.upsert_participant(predecessor)
    spawner = Spawner(registry)

    reservation = await spawner.reserve(
        SpawnRequest(
            harness="resume-spawn-test",
            prompt="do thing",
            cwd="/tmp",
            approval="edits",
            resume="sess-abc",
            description="",
        )
    )
    successor = reservation.participant

    assert successor.description is None


async def test_failed_resume_reserve_emits_no_boundary(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    predecessor = trusted_resume(registry, harness="resume-spawn-test")
    spawner = Spawner(registry)
    monkeypatch.setattr(
        spawner,
        "_build_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("plan exploded")),
    )
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )

    with pytest.raises(RuntimeError, match="plan exploded"):
        await spawner.reserve(req)

    assert not any(
        row["kind"] == BUS_KIND_PARTICIPANT_SESSION_BOUNDARY and row["from_id"] == predecessor.id
        for row in registry.store.bus_tail(limit=100)
    )


async def test_failed_reservation_releases_its_supplied_name(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    monkeypatch.setattr(
        spawner,
        "_build_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("plan exploded")),
    )

    with pytest.raises(RuntimeError, match="plan exploded"):
        await spawner.reserve(
            SpawnRequest(
                harness="resume-spawn-test",
                prompt="do thing",
                cwd="/tmp",
                approval="edits",
                name="Released-Name",
            )
        )

    replacement = registry.create_spawned(
        harness="resume-spawn-test",
        cwd="/tmp",
        name="Released-Name",
    )
    assert replacement.name == "Released-Name"


async def test_resume_none_does_not_reach_plan_launch(registry, resume_harness, monkeypatch):
    """A None resume is not forwarded, identical to the model contract."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
    )
    await spawner.reserve(req)
    assert resume_harness.seen_resume is None


async def test_check_resume_refuses_unsupported_harness(registry, no_resume_harness, monkeypatch):
    """A resume asked of a harness without the parameter is refused up front."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="no-resume-spawn-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="does not support resume"):
        await spawner.reserve(req)


async def test_resume_with_prompt_refused_for_dropping_harness(
    registry, drops_prompt_harness, monkeypatch
):
    """opencode accepts -s but drops --prompt; both must not be passed.

    Option (b): refuse with BadRequest, naming the harness and pointing at
    the alternative (resume without a prompt, then send).  Refused before
    the participant or worktree exists, so nothing is left behind.
    """
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="drops-prompt-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="cannot resume a session with a prompt"):
        await spawner.reserve(req)


async def test_resume_without_prompt_allowed_for_dropping_harness(
    registry, drops_prompt_harness, monkeypatch
):
    """Resuming without a prompt is fine even for a harness that drops prompts.

    The trap is specifically both resume AND prompt; resume alone has no
    prompt to drop.
    """
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    trusted_resume(registry, harness="drops-prompt-test")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="drops-prompt-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    await spawner.reserve(req)
    assert drops_prompt_harness.seen_resume == "sess-abc"


async def test_resume_with_response_format_refused_before_side_effects(
    registry, drops_prompt_harness, monkeypatch
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
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
        await spawner.reserve(req)
    assert registry.list(include_dead=True) == []


async def test_resume_with_worktree_refused(registry, resume_harness, monkeypatch):
    """A resumed session's transcript describes files at its original cwd;
    a fresh worktree points it at different files."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
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
        await spawner.reserve(req)
    trusted_resume(registry, harness="resume-spawn-test")
    with pytest.raises(BadRequest, match="omit worktree and pass cwd='/tmp'"):
        await spawner.reserve(req)


async def test_resume_with_named_worktree_refused(registry, resume_harness, monkeypatch):
    """A named worktree is also refused with resume, for the same reason."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
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
        await spawner.reserve(req)


async def test_spawn_session_schema_includes_resume(daemon):
    """The MCP tool schema exposes resume as an optional parameter."""
    from theater.mcp.server import build

    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    props = schema["spawn_session"]["properties"]
    assert "resume" in props
    assert "resume" not in schema["spawn_session"].get("required", [])


async def test_reserve_creates_participant_without_pane(registry, monkeypatch):
    """reserve mints the participant, worktree, and plan — but no tmux pane."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="vibe",
        prompt="say hello",
        cwd="/tmp",
        approval="edits",
    )
    reservation = await spawner.reserve(req)

    # The participant exists but has no pane yet.
    p = registry.get(reservation.participant.id)
    assert p is not None
    assert p.tmux_pane is None
    assert p.status.value == "idle"

    # The reservation carries everything launch needs.
    assert reservation.participant.id == p.id
    assert reservation.plan is not None
    assert reservation.child_cwd == "/tmp"
    assert reservation.req is req


async def test_reserve_failure_marks_participant_dead(registry, monkeypatch):
    """A failure during reserve (after participant creation) cleans up."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")

    # Sabotage plan_launch to fail after the participant exists.
    def boom(*args, **kwargs):
        raise RuntimeError("plan exploded")

    monkeypatch.setattr("theater.daemon.spawning.planning.plan_launch", boom)

    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="vibe",
        prompt="say hello",
        cwd="/tmp",
        approval="edits",
    )
    with pytest.raises(RuntimeError, match="plan exploded"):
        await spawner.reserve(req)

    # The participant was created, then marked dead by cleanup.
    participants = registry.list(include_dead=True)
    assert len(participants) == 1
    assert participants[0].status.value == "dead"


async def test_reserve_writes_config_files(registry, monkeypatch):
    """Config files are written during reserve, not deferred to launch.

    Uses the claude harness, which writes its MCP config into plan.files.
    """
    from theater import paths

    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="claude",
        prompt="say hello",
        cwd="/tmp",
        approval="manual",
    )
    reservation = await spawner.reserve(req)

    config = paths.mcp_config_path(reservation.participant.id)
    assert config.exists()
    assert reservation.participant.id in config.read_text()


def test_supports_resume_reads_the_declared_capability(resume_harness):
    assert supports_resume(resume_harness) is True


def test_supports_resume_false_when_parameter_absent(no_resume_harness):
    assert supports_resume(no_resume_harness) is False


def test_explicit_launch_parameter_support_enables_optional_parameters(no_resume_harness):
    no_resume_harness.launch_parameter_support = LaunchParameterSupport(
        model=True,
        reasoning_effort=True,
        resume=True,
    )

    assert supports_model(no_resume_harness) is True
    assert supports_reasoning(no_resume_harness) is True
    assert supports_resume(no_resume_harness) is True


def test_explicit_launch_parameter_support_can_disable_optional_parameters(resume_harness):
    resume_harness.launch_parameter_support = LaunchParameterSupport()

    assert supports_model(resume_harness) is False
    assert supports_reasoning(resume_harness) is False
    assert supports_resume(resume_harness) is False


def test_check_resume_refuses_a_harness_without_the_parameter(no_resume_harness):
    with pytest.raises(BadRequest, match="does not support resume"):
        check_resume("no-resume-spawn-test", "some-session-id")


def test_plan_launch_forwards_resume_only_when_non_none(resume_harness, tmp_path):
    """The compat contract: resume is forwarded as a keyword only when set.

    Identical to how model is forwarded. A None resume means 'no preference',
    and the plugin is never called with the keyword — so an adapter written
    before resume existed keeps working for every launch that does not name
    a session.
    """
    plan_launch(
        "resume-spawn-test",
        participant_id="abc",
        prompt="hello",
        config_path=tmp_path / "x.json",
        approval="manual",
    )
    assert resume_harness.seen_resume is None

    plan_launch(
        "resume-spawn-test",
        participant_id="abc",
        prompt="hello",
        config_path=tmp_path / "x.json",
        approval="manual",
        resume="sess-123",
    )
    assert resume_harness.seen_resume == "sess-123"


def test_plan_launch_refuses_resume_for_a_harness_without_it(no_resume_harness, tmp_path):
    with pytest.raises(BadRequest, match="does not support resume"):
        plan_launch(
            "no-resume-spawn-test",
            participant_id="abc",
            prompt="hello",
            config_path=tmp_path / "x.json",
            approval="manual",
            resume="sess-123",
        )


def test_plan_launch_without_resume_works_for_a_harness_without_it(no_resume_harness, tmp_path):
    """A harness that predates resume still launches when no resume is asked."""
    plan = plan_launch(
        "no-resume-spawn-test",
        participant_id="abc",
        prompt="hello",
        config_path=tmp_path / "x.json",
        approval="manual",
    )
    assert plan.argv == ["no-resume-spawn-test", "abc"]
