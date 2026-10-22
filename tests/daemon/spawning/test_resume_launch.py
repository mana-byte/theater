"""Resume capability checks and forwarding from the spawn RPC down to plan_launch."""

from __future__ import annotations

import pytest

from tests.support.launch_preparation import prepare_provider_reservation
from tests.support.resume_harnesses import (
    DropsPromptHarness,
    NoResumeHarness,
    ResumeHarness,
    trusted_resume,
)
from theater.constants.daemon import BUS_KIND_PARTICIPANT_SESSION_BOUNDARY
from theater.daemon.spawner import Spawner, SpawnRequest
from theater.daemon.spawning.provider_launch import ParticipantLaunchService
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
    successor = (await prepare_provider_reservation(spawner, req)).participant
    assert resume_harness.seen_resume == "sess-abc"
    assert successor.resumed_from_id == predecessor.id


async def test_resume_inherits_description_but_not_name(
    daemon, terminal_provider, resume_harness, monkeypatch
):
    registry = daemon.registry
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    predecessor = trusted_resume(registry, harness="resume-spawn-test")
    predecessor = registry.get(predecessor.id)
    predecessor.description = "Preserve this purpose"
    registry.store.upsert_participant(predecessor)
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="operator-a",
        idempotency_key="resume-description-inherit",
        params={
            "harness": "resume-spawn-test",
            "prompt": "do thing",
            "cwd": "/tmp",
            "approval": "edits",
            "resume": "sess-abc",
            "name": "New-Live-Name",
        },
    )
    operation, timed_out = await daemon.operation_service.wait(str(accepted["operation_id"]))
    assert not timed_out and operation.state == "succeeded"
    successor = registry.get(str(accepted["participant_id"]))

    assert successor.description == "Preserve this purpose"
    assert successor.name == "New-Live-Name"


async def test_resume_empty_description_inherits_in_provider_launch(
    daemon, terminal_provider, resume_harness, monkeypatch
):
    registry = daemon.registry
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    predecessor = trusted_resume(registry, harness="resume-spawn-test")
    predecessor = registry.get(predecessor.id)
    predecessor.description = "Clear this purpose"
    registry.store.upsert_participant(predecessor)
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="operator-a",
        idempotency_key="resume-description-clear",
        params={
            "harness": "resume-spawn-test",
            "prompt": "do thing",
            "cwd": "/tmp",
            "approval": "edits",
            "resume": "sess-abc",
            "description": "",
        },
    )
    operation, timed_out = await daemon.operation_service.wait(str(accepted["operation_id"]))
    assert not timed_out and operation.state == "succeeded"
    successor = registry.get(str(accepted["participant_id"]))

    assert successor.description == "Clear this purpose"


async def test_provider_admission_rechecks_prevalidated_resume_identity(
    daemon, terminal_provider, resume_harness, monkeypatch
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    registry = daemon.registry
    predecessor = trusted_resume(registry, harness="resume-spawn-test")
    original = daemon.workspace_service.prepare_for_spawn

    async def move_identity(*args, **kwargs):
        changed = registry.get(predecessor.id)
        changed.transcript_domain = "/changed-domain"
        registry.store.upsert_participant(changed)
        return await original(*args, **kwargs)

    monkeypatch.setattr(daemon.workspace_service, "prepare_for_spawn", move_identity)
    with pytest.raises(BadRequest, match="resume identity changed"):
        await ParticipantLaunchService(daemon).spawn(
            client_id="operator-a",
            idempotency_key="resume-stale-identity",
            params={
                "harness": "resume-spawn-test",
                "prompt": "continue",
                "cwd": "/tmp",
                "approval": "edits",
                "resume": "sess-abc",
            },
        )
    assert [row.id for row in registry.list(include_dead=True)] == [predecessor.id]
    assert terminal_provider.creations == []


async def test_failed_resume_preparation_emits_no_boundary(registry, resume_harness, monkeypatch):
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
        await prepare_provider_reservation(spawner, req)

    assert not any(
        row["kind"] == BUS_KIND_PARTICIPANT_SESSION_BOUNDARY and row["from_id"] == predecessor.id
        for row in registry.store.bus_tail(limit=100)
    )


async def test_failed_preparation_releases_its_supplied_name(
    daemon, terminal_provider, monkeypatch
):
    monkeypatch.setattr(
        daemon.spawner,
        "_build_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("plan exploded")),
    )
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="operator-a",
        idempotency_key="failed-named-preparation",
        params={
            "harness": "vibe",
            "prompt": "do thing",
            "cwd": "/tmp",
            "approval": "edits",
            "name": "Released-Name",
        },
    )
    operation, _timed_out = await daemon.operation_service.wait(str(accepted["operation_id"]))
    assert operation.state == "failed"
    assert daemon.registry.get(str(accepted["participant_id"])).status.value == "dead"
    assert terminal_provider.creations == []

    replacement = daemon.registry.create_spawned(
        harness="vibe",
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
    await prepare_provider_reservation(spawner, req)
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
        await prepare_provider_reservation(spawner, req)


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
        await prepare_provider_reservation(spawner, req)


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
    await prepare_provider_reservation(spawner, req)
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
        await prepare_provider_reservation(spawner, req)
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
        await prepare_provider_reservation(spawner, req)
    trusted_resume(registry, harness="resume-spawn-test")
    with pytest.raises(BadRequest, match="omit worktree and pass cwd='/tmp'"):
        await prepare_provider_reservation(spawner, req)


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
        await prepare_provider_reservation(spawner, req)


async def test_spawn_session_schema_includes_resume(daemon):
    """The MCP tool schema exposes resume as an optional parameter."""
    from theater.mcp.server import build

    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    props = schema["spawn_session"]["properties"]
    assert "resume" in props
    assert "resume" not in schema["spawn_session"].get("required", [])


async def test_provider_preparation_keeps_participant_unbound(registry, monkeypatch):
    """Provider preparation builds the plan before terminal creation."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="vibe",
        prompt="say hello",
        cwd="/tmp",
        approval="edits",
    )
    reservation = await prepare_provider_reservation(spawner, req)

    # The participant exists but has no pane yet.
    p = registry.get(reservation.participant.id)
    assert p is not None
    assert p.tmux_pane is None
    assert p.status.value == "idle"

    # The reservation carries everything launch needs.
    assert reservation.participant.id == p.id
    assert reservation.plan is not None
    assert reservation.child_cwd == "/tmp"
    assert reservation.req == req


async def test_provider_preparation_writes_config_files(registry, monkeypatch):
    """Config files are written during preparation, before terminal creation.

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
    reservation = await prepare_provider_reservation(spawner, req)

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
