"""The generic resume overlay hook: env/cwd overlays, alias-stored harnesses and rejected plans."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.builders import alias_predecessor
from tests.support.launch_preparation import prepare_provider_reservation
from tests.support.resume_harnesses import trusted_predecessor
from theater.daemon.spawner import Spawner, SpawnRequest
from theater.daemon.spawning.provider_launch import ParticipantLaunchService
from theater.daemon.worktrees.service import WorkspaceRequest
from theater.harness import HARNESSES, Harness, LaunchPlan
from theater.harness.base import ResumeLaunchOverlay
from theater.harness.builtin.plugins.vibe.constants import _MARKER_KEY, ISOLATION_MARKER
from theater.harness.builtin.plugins.vibe.isolation import (
    isolation_marker_text,
    validate_isolated_domain,
)
from theater.harness.contracts.harness import LaunchParameterSupport
from theater.models import BadRequest


class _OverlayHarness(Harness):
    """A fake harness that implements resume_launch_overlay with a custom env."""

    name = "overlay-test"
    binary = "overlay-test"
    icon = "O"
    launch_parameter_support = LaunchParameterSupport(model=True, resume=True)

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

    seen_plan_env: dict | None = None
    seen_resume: str | None = None
    resume_cwd: str | None = None
    resume_reference: str | None = None

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
        plan = LaunchPlan(argv=["overlay-test"], env={"PLAN_KEY": "plan"})
        self.seen_plan_env = dict(plan.env)
        self.seen_resume = resume
        return plan

    def resume_launch_overlay(self, *, predecessor, trusted_session_owners):
        return ResumeLaunchOverlay(
            env={"OVERLAY_KEY": "overlay", "PLAN_KEY": "overlay-wins"},
            transcript_domain="/tmp/overlay-domain",
            cwd=self.resume_cwd,
            resume_reference=self.resume_reference,
        )


class _PermissiveHarness(Harness):
    """A harness with no resume_launch_overlay override — uses the base."""

    name = "permissive-test"
    binary = "permissive-test"
    icon = "P"
    launch_parameter_support = LaunchParameterSupport(model=True, resume=True)

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
        model: str | None = None,
        resume: str | None = None,
    ) -> LaunchPlan:
        return LaunchPlan(
            argv=["permissive-test"],
            env={"PLAN_KEY": "plan"},
            transcript_domain="/tmp/plan-domain",
        )


@pytest.fixture
def overlay_harness(monkeypatch):
    h = _OverlayHarness()
    monkeypatch.setitem(HARNESSES, "overlay-test", h)
    return h


@pytest.fixture
def permissive_harness(monkeypatch):
    h = _PermissiveHarness()
    monkeypatch.setitem(HARNESSES, "permissive-test", h)
    return h


async def test_overlay_env_reaches_launch_plan(
    registry, overlay_harness, monkeypatch, terminal_provider
):
    """A non-Vibe harness implementing resume_launch_overlay drives a
    resume end to end, with overlay env reaching the launch plan."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    trusted_predecessor(registry, harness="overlay-test")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="overlay-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    reservation = await prepare_provider_reservation(spawner, req)
    assert reservation.plan.env["OVERLAY_KEY"] == "overlay"
    # Overlay wins on conflict.
    assert reservation.plan.env["PLAN_KEY"] == "overlay-wins"
    # plan.env was not mutated — the spawner keeps the original plan intact.
    assert overlay_harness.seen_plan_env == {"PLAN_KEY": "plan"}


async def test_overlay_cwd_replaces_request_before_provider_preparation(
    registry, overlay_harness, monkeypatch, terminal_provider, tmp_path
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    requested = tmp_path / "requested"
    authoritative = tmp_path / "authoritative"
    requested.mkdir()
    authoritative.mkdir()
    overlay_harness.resume_cwd = str(authoritative)
    trusted_predecessor(registry, harness="overlay-test")

    reservation = await prepare_provider_reservation(
        Spawner(registry),
        SpawnRequest(
            harness="overlay-test",
            prompt="",
            cwd=str(requested),
            approval="edits",
            resume="sess-abc",
        ),
    )

    spawned = reservation.participant
    assert reservation.child_cwd == str(authoritative)
    assert registry.get(spawned.id).cwd == str(authoritative)


async def test_overlay_resume_reference_replaces_only_the_planner_input(
    registry, overlay_harness, monkeypatch, terminal_provider
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    overlay_harness.resume_reference = "/trusted/native/transcript.jsonl"
    predecessor = trusted_predecessor(registry, harness="overlay-test")

    reservation = await prepare_provider_reservation(
        Spawner(registry),
        SpawnRequest(
            harness="overlay-test",
            prompt="",
            cwd="/tmp",
            approval="edits",
            resume="sess-abc",
        ),
    )

    spawned = reservation.participant
    assert overlay_harness.seen_resume == "/trusted/native/transcript.jsonl"
    assert registry.get(spawned.id).resumed_from_id == predecessor.id


async def test_base_overlay_refuses_predecessor_with_domain(
    registry, permissive_harness, monkeypatch
):
    """The base default refuses when a predecessor has a transcript_domain
    but the harness does not implement resume_launch_overlay."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    trusted_predecessor(registry, harness="permissive-test", transcript_domain="/tmp/some-domain")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="permissive-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="does not implement resume_launch_overlay"):
        await prepare_provider_reservation(spawner, req)


async def test_overlay_env_wins_and_plan_env_not_mutated(
    registry, overlay_harness, monkeypatch, terminal_provider
):
    """Overlay env wins over plan env on a conflicting key, and
    plan.env is not mutated by the merge."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    trusted_predecessor(registry, harness="overlay-test")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="overlay-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    reservation = await prepare_provider_reservation(spawner, req)
    # plan.env was not mutated.
    assert overlay_harness.seen_plan_env == {"PLAN_KEY": "plan"}
    assert reservation.plan.env["PLAN_KEY"] == "overlay-wins"
    assert reservation.plan.env["OVERLAY_KEY"] == "overlay"


async def test_overlay_none_transcript_domain_preserves_plan_domain(
    registry, monkeypatch, terminal_provider
):
    """Transcript_domain=None in the overlay preserves the plan's
    domain rather than clearing it."""

    class _NullDomainHarness(Harness):
        name = "null-domain-test"
        binary = "null-domain-test"
        icon = "N"
        launch_parameter_support = LaunchParameterSupport(model=True, resume=True)

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
            self, *, participant_id, prompt, config_path, approval, model=None, resume=None
        ):
            return LaunchPlan(
                argv=["null-domain-test"],
                transcript_domain="/tmp/plan-domain",
            )

        def resume_launch_overlay(self, *, predecessor, trusted_session_owners):
            # Returns None transcript_domain — must NOT clear the plan's domain.
            return ResumeLaunchOverlay()

    monkeypatch.setitem(HARNESSES, "null-domain-test", _NullDomainHarness())
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    trusted_predecessor(registry, harness="null-domain-test", transcript_domain="/tmp/any")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="null-domain-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    spawned = (await prepare_provider_reservation(spawner, req)).participant
    # The plan's domain was preserved, not cleared to None.
    assert registry.get(spawned.id).transcript_domain == "/tmp/plan-domain"


async def test_alias_resolves_at_validate_resume_identity(registry, monkeypatch, terminal_provider):
    """An alias-stored harness row is found by _validate_resume_identity."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    alias_predecessor(registry, alias="claude-code", canonical="claude", session_id="sess-alias-1")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="claude",
        prompt="",
        cwd="/tmp",
        approval="manual",
        resume="sess-alias-1",
    )
    # The spawn must NOT raise "no trusted" — the alias-stored row must be
    # found by canonical comparison. A raw comparison would miss it.
    # Claude's resume_launch_overlay may raise for a domainless predecessor,
    # but that is a *different* error than "no trusted". We assert positively:
    # the identity gate passed, meaning the predecessor was found.
    try:
        await prepare_provider_reservation(spawner, req)
    except BadRequest as exc:
        if "no trusted" in str(exc):
            pytest.fail("alias-stored row was not found at _validate_resume_identity: " + str(exc))


async def test_alias_resolves_at_resume_state_peer_scan(registry, monkeypatch):
    """An alias-stored live peer is found by the _resume_state peer scan ("owned_by_live")."""
    from theater.daemon.methods import _resume_state

    # A live peer stored under alias "claude-code" should be found when
    # checking a dead row stored under canonical "claude".
    alias_predecessor(
        registry,
        alias="claude-code",
        canonical="claude",
        session_id="sess-peer-1",
        live=True,
        pane=None,
    )
    # The subject row is dead, stored under canonical "claude".
    dead = registry.register(
        harness="claude",
        pane=None,
        cwd="/tmp",
        session_id="sess-peer-1",
    )
    dead.session_correlation = "exact"
    registry.store.upsert_participant(dead)
    registry.mark_dead(dead.id)
    live_peers = registry.list(include_dead=False)
    dead = registry.get(dead.id)
    state = _resume_state(dead, live_peers)
    assert state == "owned_by_live", (
        f"alias-stored live peer was not found; got {state!r}. "
        "The peer scan must use normalize() to match an alias-stored row."
    )


async def test_alias_resolves_at_resolve_resume_reference(registry, monkeypatch, terminal_provider):
    """An alias-stored row matches the request in _resolve_resume_reference (resume=<id>)."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    p = alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-alias-pid"
    )
    # Resume by participant id with the canonical name "claude".
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="claude",
        prompt="",
        cwd="/tmp",
        approval="manual",
        resume=p.id,
    )
    # The spawn must NOT raise "belongs to harness" — the alias is
    # canonically "claude" and _resolve_resume_reference must accept it.
    try:
        await prepare_provider_reservation(spawner, req)
    except BadRequest as exc:
        if "belongs to harness" in str(exc):
            pytest.fail("alias-stored row was rejected at _resolve_resume_reference: " + str(exc))


def test_validation_does_not_create_marker_key(tmp_path, monkeypatch):
    """Validating when no key exists must return invalid without
    creating the key file."""
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    from theater import paths

    paths.ensure_home()
    key_path = paths.keys_dir() / _MARKER_KEY
    # Create a marker first (this creates the key), then delete the key so
    # validation must cope without it.
    domain = tmp_path / "domain"
    domain.mkdir()
    marker_path = domain / ISOLATION_MARKER
    marker_path.write_text(
        isolation_marker_text(participant_id="p1", transcript_domain=domain),
        encoding="utf-8",
    )
    # The marker was signed with a key that no longer exists.
    key_path.unlink()
    assert not key_path.exists()
    # Validation must return None (invalid) because no key exists.
    result = validate_isolated_domain(domain)
    assert result is None
    # The key file must still be absent.
    assert not key_path.exists(), "validation created the marker key file"


def _init_repo(path: Path) -> Path:
    """Create a real git repo for worktree tests."""
    import subprocess

    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    (path / "README").write_text("init")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


async def test_rejected_plan_removes_its_new_named_workspace(
    daemon, terminal_provider, monkeypatch, tmp_path
):
    import subprocess

    repo = _init_repo(tmp_path / "repo")

    def reject(_plan, _participant):
        raise BadRequest("plan rejected by pre-flight")

    monkeypatch.setattr(daemon.spawner, "_validate_receipt_plan", reject)
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="operator-a",
        idempotency_key="rejected-new-named",
        params={
            "harness": "vibe",
            "prompt": "say hello",
            "approval": "edits",
            "workspace": {"cwd": str(repo), "worktree": "doomed-name"},
        },
    )
    operation, _timed_out = await daemon.operation_service.wait(str(accepted["operation_id"]))
    assert operation.state == "failed"
    participant = daemon.registry.get(str(accepted["participant_id"]))
    workspace = daemon.store.workspaces.get(participant.workspace_id)
    assert workspace is not None and workspace.state == "removed"
    assert not Path(workspace.path).exists()
    assert terminal_provider.creations == []

    result = subprocess.run(  # noqa: ASYNC221
        ["git", "branch", "--list", "theater/named/*"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "theater/named/doomed-name" not in result.stdout


async def test_rejected_plan_preserves_an_existing_named_worktree(
    daemon, terminal_provider, monkeypatch, tmp_path
):
    import subprocess

    repo = _init_repo(tmp_path / "repo")
    existing = await daemon.workspace_service.reserve(
        WorkspaceRequest(cwd=str(repo), worktree="retained-name"), reservation_id="seed-named"
    )
    daemon.workspace_service.release_usage(existing.usage.usage_id, reason="seed_complete")

    def reject(_plan, _participant):
        raise BadRequest("plan rejected")

    monkeypatch.setattr(daemon.spawner, "_validate_receipt_plan", reject)
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="operator-a",
        idempotency_key="rejected-joined-named",
        params={
            "harness": "vibe",
            "prompt": "say hello",
            "approval": "edits",
            "workspace": {"cwd": str(repo), "worktree": "retained-name"},
        },
    )
    operation, _timed_out = await daemon.operation_service.wait(str(accepted["operation_id"]))
    assert operation.state == "failed"
    participant = daemon.registry.get(str(accepted["participant_id"]))
    assert participant.workspace_id == existing.workspace.workspace_id
    assert terminal_provider.creations == []

    assert Path(existing.workspace.path).is_dir()
    result = subprocess.run(  # noqa: ASYNC221
        ["git", "rev-parse", "--verify", "theater/named/retained-name"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
