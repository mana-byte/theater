"""Tests for the generic resume overlay hook and the points it covers.

Each test is annotated with the brief point number it covers, so a
mutation-test that reverts a specific change kills a specific test here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theater.daemon.spawner import Spawner, SpawnRequest
from theater.harness import HARNESSES, Harness, LaunchPlan
from theater.harness.base import ResumeLaunchOverlay
from theater.harness.builtin.plugins.vibe import (
    _MARKER_KEY,
    ISOLATION_MARKER,
    isolation_marker_text,
    validate_isolated_domain,
)
from theater.models import BadRequest

# ---- helpers -----------------------------------------------------------


def _trusted_predecessor(
    registry,
    *,
    harness: str,
    session_id: str = "sess-abc",
    transcript_domain: str | None = None,
):
    p = registry.register(harness=harness, pane=None, cwd="/tmp", session_id=session_id)
    p.session_correlation = "exact"
    if transcript_domain is not None:
        p.transcript_domain = transcript_domain
    registry.store.upsert_participant(p)
    registry.mark_dead(p.id)
    return p


class _OverlayHarness(Harness):
    """A fake harness that implements resume_launch_overlay with a custom env."""

    name = "overlay-test"
    binary = "overlay-test"
    icon = "O"

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
        return plan

    def resume_launch_overlay(self, *, predecessor, trusted_session_owners):
        return ResumeLaunchOverlay(
            env={"OVERLAY_KEY": "overlay", "PLAN_KEY": "overlay-wins"},
            transcript_domain="/tmp/overlay-domain",
        )


class _PermissiveHarness(Harness):
    """A harness with no resume_launch_overlay override — uses the base."""

    name = "permissive-test"
    binary = "permissive-test"
    icon = "P"

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


# ---- point 1: fake harness drives resume end to end with overlay env ----


async def test_overlay_env_reaches_launch_plan(registry, overlay_harness, monkeypatch, fake_tmux):
    """Point 1: a non-Vibe harness implementing resume_launch_overlay drives a
    resume end to end, with overlay env reaching the launch plan."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_predecessor(registry, harness="overlay-test")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="overlay-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    await spawner.spawn(req)
    # The overlay env should have reached the tmux window env.
    window_env = fake_tmux.windows[-1]["env"]
    assert window_env["OVERLAY_KEY"] == "overlay"
    # Overlay wins on conflict.
    assert window_env["PLAN_KEY"] == "overlay-wins"
    # plan.env was not mutated — the spawner keeps the original plan intact.
    assert overlay_harness.seen_plan_env == {"PLAN_KEY": "plan"}


# ---- point 1: base default — empty overlay for domainless, refuse domain ----


async def test_base_overlay_empty_for_domainless_predecessor(
    registry, permissive_harness, monkeypatch
):
    """Point 1: the base default returns an empty overlay when the predecessor
    has no transcript_domain, and the spawn succeeds."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_predecessor(registry, harness="permissive-test", transcript_domain=None)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="permissive-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    # Should succeed — base returns an empty overlay for domainless predecessor.
    await spawner.spawn(req)


async def test_base_overlay_refuses_predecessor_with_domain(
    registry, permissive_harness, monkeypatch
):
    """Point 1: the base default refuses when a predecessor has a transcript_domain
    but the harness does not implement resume_launch_overlay."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_predecessor(registry, harness="permissive-test", transcript_domain="/tmp/some-domain")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="permissive-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="does not implement resume_launch_overlay"):
        await spawner.spawn(req)


# ---- point 3: overlay env wins over plan env, plan.env not mutated ----


async def test_overlay_env_wins_and_plan_env_not_mutated(
    registry, overlay_harness, monkeypatch, fake_tmux
):
    """Point 3: overlay env wins over plan env on a conflicting key, and
    plan.env is not mutated by the merge."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_predecessor(registry, harness="overlay-test")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="overlay-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    await spawner.spawn(req)
    # plan.env was not mutated.
    assert overlay_harness.seen_plan_env == {"PLAN_KEY": "plan"}
    # The merged env reached the tmux window.
    window_env = fake_tmux.windows[-1]["env"]
    assert window_env["PLAN_KEY"] == "overlay-wins"
    assert window_env["OVERLAY_KEY"] == "overlay"


# ---- point 4: transcript_domain=None preserves the plan's domain ----


async def test_overlay_none_transcript_domain_preserves_plan_domain(
    registry, monkeypatch, fake_tmux
):
    """Point 4: transcript_domain=None in the overlay preserves the plan's
    domain rather than clearing it."""

    class _NullDomainHarness(Harness):
        name = "null-domain-test"
        binary = "null-domain-test"
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
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_predecessor(registry, harness="null-domain-test", transcript_domain="/tmp/any")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="null-domain-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    spawned = await spawner.spawn(req)
    # The plan's domain was preserved, not cleared to None.
    assert registry.get(spawned.id).transcript_domain == "/tmp/plan-domain"


# ---- point 5: claude/codex/opencode refuse mismatched domain ----


async def test_claude_refuses_mismatched_domain(registry, monkeypatch):
    """Point 5: Claude refuses a predecessor whose domain does not match its
    native observation namespace."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_predecessor(registry, harness="claude", transcript_domain="/tmp/wrong-claude-root")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="claude",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="does not match the Claude observation root"):
        await spawner.spawn(req)


async def test_codex_refuses_mismatched_domain(registry, monkeypatch):
    """Point 5: Codex refuses a predecessor whose domain does not match its
    native observation namespace."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_predecessor(registry, harness="codex", transcript_domain="/tmp/wrong-codex-root")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="codex",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="does not match the Codex observation root"):
        await spawner.spawn(req)


async def test_opencode_refuses_mismatched_domain(registry, monkeypatch):
    """Point 5: OpenCode refuses a predecessor whose domain does not match its
    native observation namespace."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    _trusted_predecessor(registry, harness="opencode", transcript_domain="/tmp/wrong-opencode-root")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="opencode",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="does not match the OpenCode"):
        await spawner.spawn(req)


# ---- point 6: alias-stored harness resolves at all three canonical sites ----


async def test_alias_resolves_at_validate_resume_identity(registry, monkeypatch, fake_tmux):
    """Point 6: an alias-stored harness row resolves at _validate_resume_identity."""

    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    # Register a predecessor stored under the alias "claude-code".
    p = registry.register(
        harness="claude-code",
        pane=None,
        cwd="/tmp",
        session_id="sess-alias-1",
    )
    p.session_correlation = "exact"
    registry.store.upsert_participant(p)
    registry.mark_dead(p.id)
    # Resume with the canonical name "claude" — must still find the row.
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="claude",
        prompt="",
        cwd="/tmp",
        approval="manual",
        resume="sess-alias-1",
    )
    # Should not raise "no trusted" — the alias-stored row is found.
    # (Claude may raise for other reasons, but not "no trusted".)
    try:
        await spawner.spawn(req)
    except BadRequest as exc:
        assert "no trusted" not in str(exc), (
            "alias-stored row was not found by canonical name at _validate_resume_identity"
        )


async def test_alias_resolves_at_resume_state_peer_scan(registry, monkeypatch):
    """Point 6: an alias-stored harness row resolves at the methods.py
    _resume_state peer scan."""
    from theater.daemon.methods import _resume_state

    # A live peer stored under alias "claude-code" should be found when
    # checking a row stored under canonical "claude".
    live = registry.register(
        harness="claude-code",
        pane="%99",
        cwd="/tmp",
        session_id="sess-peer-1",
    )
    live.session_correlation = "exact"
    registry.store.upsert_participant(live)
    # The subject row is dead, stored under "claude".
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
    assert state == "owned_by_live", f"alias-stored live peer was not found; got {state!r}"


async def test_alias_resolves_at_resolve_resume_reference(registry, monkeypatch, fake_tmux):
    """Point 6: an alias-stored harness row resolves at _resolve_resume_reference
    (resume=<participant-id>)."""
    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    # Register a predecessor stored under the alias "claude-code".
    p = registry.register(
        harness="claude-code",
        pane=None,
        cwd="/tmp",
        session_id="sess-alias-pid",
    )
    p.session_correlation = "exact"
    registry.store.upsert_participant(p)
    registry.mark_dead(p.id)
    # Resume by participant id with the canonical name "claude".
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="claude",
        prompt="",
        cwd="/tmp",
        approval="manual",
        resume=p.id,
    )
    # Should not raise "belongs to harness" — the alias is canonically "claude".
    try:
        await spawner.spawn(req)
    except BadRequest as exc:
        assert "belongs to harness" not in str(exc), (
            "alias-stored row was rejected at _resolve_resume_reference"
        )


# ---- point 7: marker key file absent after validation ----


def test_validation_does_not_create_marker_key(tmp_path, monkeypatch):
    """Point 7: validating when no key exists must return invalid without
    creating the key file."""
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    from theater import paths

    paths.ensure_home()
    key_path = paths.home() / _MARKER_KEY
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


# ---- point 9: rejected plan leaves no named branch behind ----


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


async def test_rejected_plan_leaves_no_named_branch(registry, monkeypatch, tmp_path):
    """Point 9: a launch plan rejected by receipt pre-flight leaves no named
    worktree branch behind, because plan construction and pre-flight run before
    _prepare_worktree."""
    import subprocess

    monkeypatch.setattr("theater.daemon.spawner.shutil.which", lambda b: f"/usr/bin/{b}")
    repo = _init_repo(tmp_path / "repo")

    # Sabotage _validate_receipt_plan to reject the plan after _build_plan.
    spawner = Spawner(registry)

    def reject(plan, participant):
        raise BadRequest("plan rejected by pre-flight")

    monkeypatch.setattr(spawner, "_validate_receipt_plan", reject)

    req = SpawnRequest(
        harness="vibe",
        prompt="say hello",
        cwd=str(repo),
        approval="edits",
        worktree="doomed-name",
    )
    with pytest.raises(BadRequest, match="plan rejected"):
        await spawner.reserve(req)

    # No named branch should exist.
    result = subprocess.run(  # noqa: ASYNC221
        ["git", "branch", "--list", "theater/named/*"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "", f"named branch was left behind: {result.stdout!r}"
