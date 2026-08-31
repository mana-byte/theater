"""Tests for the generic resume overlay hook and the points it covers.

Each test is annotated with the brief point number it covers, so a
mutation-test that reverts a specific change kills a specific test here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shipped import ClaudeCodeHarness

from theater.daemon.spawner import Spawner, SpawnRequest
from theater.harness import HARNESSES, Harness, LaunchPlan
from theater.harness.base import ResumeLaunchOverlay
from theater.harness.builtin.plugins.vibe.constants import _MARKER_KEY, ISOLATION_MARKER
from theater.harness.builtin.plugins.vibe.isolation import (
    isolation_marker_text,
    validate_isolated_domain,
)
from theater.harness.contracts.harness import LaunchParameterSupport
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
    return registry.get(p.id)


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


# ---- point 1: fake harness drives resume end to end with overlay env ----


async def test_overlay_env_reaches_launch_plan(registry, overlay_harness, monkeypatch, fake_tmux):
    """Point 1: a non-Vibe harness implementing resume_launch_overlay drives a
    resume end to end, with overlay env reaching the launch plan."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
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


async def test_overlay_cwd_replaces_request_before_reservation(
    registry, overlay_harness, monkeypatch, fake_tmux, tmp_path
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    requested = tmp_path / "requested"
    authoritative = tmp_path / "authoritative"
    requested.mkdir()
    authoritative.mkdir()
    overlay_harness.resume_cwd = str(authoritative)
    _trusted_predecessor(registry, harness="overlay-test")

    spawned = await Spawner(registry).spawn(
        SpawnRequest(
            harness="overlay-test",
            prompt="",
            cwd=str(requested),
            approval="edits",
            resume="sess-abc",
        )
    )

    assert fake_tmux.windows[-1]["cwd"] == str(authoritative)
    assert registry.get(spawned.id).cwd == str(authoritative)


async def test_overlay_resume_reference_replaces_only_the_planner_input(
    registry, overlay_harness, monkeypatch, fake_tmux
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    overlay_harness.resume_reference = "/trusted/native/transcript.jsonl"
    predecessor = _trusted_predecessor(registry, harness="overlay-test")

    spawned = await Spawner(registry).spawn(
        SpawnRequest(
            harness="overlay-test",
            prompt="",
            cwd="/tmp",
            approval="edits",
            resume="sess-abc",
        )
    )

    assert overlay_harness.seen_resume == "/trusted/native/transcript.jsonl"
    assert registry.get(spawned.id).resumed_from_id == predecessor.id


# ---- point 1: base default — empty overlay for domainless, refuse domain ----


async def test_base_overlay_empty_for_domainless_predecessor(
    registry, permissive_harness, monkeypatch
):
    """Point 1: the base default returns an empty overlay when the predecessor
    has no transcript_domain, and the spawn succeeds."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
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
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
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
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
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
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
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


async def test_claude_resume_uses_latest_transcript_project_cwd(
    registry, monkeypatch, fake_tmux, tmp_path
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    root = tmp_path / ".claude" / "projects"
    original = tmp_path / "Desktop"
    current = original / "sre-infra"
    original.mkdir()
    current.mkdir()
    session_id = "f2c02c06-8864-4144-bd87-36a0f9cd33dd"
    transcript = root / "-Users-manaiki-laut-Desktop-sre-infra" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"sessionId": session_id, "cwd": str(original)},
                {"sessionId": session_id, "cwd": str(current)},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(HARNESSES, "claude", ClaudeCodeHarness(root=root))
    predecessor = _trusted_predecessor(registry, harness="claude", session_id=session_id)
    predecessor.cwd = str(original)
    predecessor.transcript_location = str(transcript)
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    successor = await Spawner(registry).spawn(
        SpawnRequest(
            harness="claude",
            prompt="continue",
            cwd=str(original),
            approval="manual",
            resume=session_id,
        )
    )

    command = fake_tmux.windows[-1]["command"]
    assert fake_tmux.windows[-1]["cwd"] == str(current)
    assert successor.cwd == str(current)
    assert f"--resume={session_id}" in command
    assert "--fork-session" in command
    fresh = next(arg for arg in command if arg.startswith("--session-id="))
    assert fresh != f"--session-id={session_id}"


async def test_claude_resume_requires_a_materialized_native_transcript(
    registry, monkeypatch, fake_tmux, tmp_path
):
    from theater.daemon.rpc.participants import _resume_state

    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    root = tmp_path / ".claude" / "projects"
    root.mkdir(parents=True)
    session_id = "f2c02c06-8864-4144-bd87-36a0f9cd33dd"
    monkeypatch.setitem(HARNESSES, "claude", ClaudeCodeHarness(root=root))
    predecessor = _trusted_predecessor(registry, harness="claude", session_id=session_id)

    assert _resume_state(predecessor, []) == "harness_resume_rejected"
    with pytest.raises(BadRequest, match="native transcript has not materialized"):
        await Spawner(registry).reserve(
            SpawnRequest(
                harness="claude",
                prompt="",
                cwd=str(tmp_path),
                approval="manual",
                resume=session_id,
            )
        )

    assert [p.id for p in registry.list(include_dead=True)] == [predecessor.id]
    assert fake_tmux.windows == []


def test_claude_resume_state_preflight_does_not_read_transcript(registry, monkeypatch, tmp_path):
    from theater.daemon.rpc.participants import _resume_state

    root = tmp_path / ".claude" / "projects"
    transcript = root / "-repo" / "sess-abc.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("not JSON\n", encoding="utf-8")
    monkeypatch.setitem(HARNESSES, "claude", ClaudeCodeHarness(root=root))
    predecessor = _trusted_predecessor(registry, harness="claude")
    predecessor.transcript_location = str(transcript)
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    def no_read(*args, **kwargs):
        raise AssertionError("resume-state preflight read transcript contents")

    monkeypatch.setattr(Path, "open", no_read)
    assert _resume_state(registry.get(predecessor.id), []) == "resumable"


def test_claude_resume_cwd_uses_latest_bounded_suffix_record(monkeypatch, tmp_path):
    from theater.harness.builtin.plugins.claude import identity

    old = tmp_path / "old"
    current = tmp_path / "current"
    old.mkdir()
    current.mkdir()
    session_id = "f2c02c06-8864-4144-bd87-36a0f9cd33dd"
    transcript = tmp_path / f"{session_id}.jsonl"
    records = [json.dumps({"session_id": session_id, "cwd": str(old)})]
    records.extend(json.dumps({"session_id": session_id, "padding": "x" * 128}) for _ in range(16))
    records.append(json.dumps({"sessionId": session_id, "cwd": str(current)}))
    transcript.write_text("\n".join(records) + "\n", encoding="utf-8")
    monkeypatch.setattr(identity, "_RESUME_CWD_SUFFIX_BYTES", 512)
    monkeypatch.setattr(identity, "_RESUME_CWD_SUFFIX_RECORDS", 8)

    assert identity._current_transcript_cwd(transcript, session_id) == str(current)


def test_claude_resume_cwd_accepts_final_record_without_newline(tmp_path):
    from theater.harness.builtin.plugins.claude import identity

    cwd = tmp_path / "current"
    cwd.mkdir()
    session_id = "f2c02c06-8864-4144-bd87-36a0f9cd33dd"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text(
        json.dumps({"sessionId": session_id, "cwd": str(cwd)}),
        encoding="utf-8",
    )

    assert identity._current_transcript_cwd(transcript, session_id) == str(cwd)


@pytest.mark.parametrize("tail", ['{"incomplete"', json.dumps({"sessionId": "sess-abc"})])
def test_claude_resume_cwd_ignores_unusable_final_record(tmp_path, tail):
    from theater.harness.builtin.plugins.claude import identity

    cwd = tmp_path / "current"
    cwd.mkdir()
    session_id = "sess-abc"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text(
        json.dumps({"sessionId": session_id, "cwd": str(cwd)}) + "\n" + tail,
        encoding="utf-8",
    )

    assert identity._current_transcript_cwd(transcript, session_id) == str(cwd)


def test_claude_resume_cwd_refuses_latest_relative_cwd(tmp_path):
    from theater.harness.builtin.plugins.claude import identity

    old = tmp_path / "old"
    old.mkdir()
    session_id = "sess-abc"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text(
        "\n".join(
            (
                json.dumps({"sessionId": session_id, "cwd": str(old)}),
                json.dumps({"sessionId": session_id, "cwd": "relative"}),
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(BadRequest, match="records a relative cwd"):
        identity._current_transcript_cwd(transcript, session_id)


def test_claude_resume_transcript_refuses_duplicate_known_location(tmp_path):
    from theater.harness.builtin.plugins.claude import identity

    root = tmp_path / ".claude" / "projects"
    session_id = "sess-abc"
    first = root / "-first" / f"{session_id}.jsonl"
    second = root / "-second" / f"{session_id}.jsonl"
    first.parent.mkdir(parents=True)
    second.parent.mkdir()
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")

    with pytest.raises(BadRequest, match="multiple native transcripts"):
        identity.materialized_resume_transcript(
            root=root,
            session_id=session_id,
            known_location=str(first),
        )


def test_vibe_resume_state_remains_generic(registry):
    from theater.daemon.rpc.participants import _resume_state

    predecessor = _trusted_predecessor(registry, harness="vibe")
    assert _resume_state(predecessor, []) == "resumable"


async def test_claude_resume_keeps_a_matching_transcript_cwd(
    registry, monkeypatch, fake_tmux, tmp_path
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    root = tmp_path / ".claude" / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    session_id = "f2c02c06-8864-4144-bd87-36a0f9cd33dd"
    transcript = root / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"session_id": session_id, "cwd": str(cwd)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(HARNESSES, "claude", ClaudeCodeHarness(root=root))
    predecessor = _trusted_predecessor(registry, harness="claude", session_id=session_id)
    predecessor.cwd = str(cwd)
    predecessor.transcript_location = str(transcript)
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    successor = await Spawner(registry).spawn(
        SpawnRequest(
            harness="claude",
            prompt="",
            cwd=str(cwd),
            approval="manual",
            resume=session_id,
        )
    )

    assert fake_tmux.windows[-1]["cwd"] == str(cwd)
    assert successor.cwd == str(cwd)


async def test_codex_refuses_mismatched_domain(registry, monkeypatch):
    """Point 5: Codex refuses a predecessor whose domain does not match its
    native observation namespace."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
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
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
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


def _alias_predecessor(registry, *, alias, canonical, session_id, live=False, pane=None):
    """Create a participant whose stored harness is an alias spelling.

    ``registry.register`` normalizes the harness at line 243, so calling it
    with ``harness="claude-code"`` stores ``"claude"`` and defeats the test.
    To get a genuinely alias-stored row we register under the canonical name,
    then mutate ``p.harness`` to the alias and re-upsert — bypassing the
    normalizer. Assert the stored spelling is the alias before returning.
    """
    p = registry.register(
        harness=canonical,
        pane=pane,
        cwd="/tmp",
        session_id=session_id,
    )
    p.harness = alias
    p.session_correlation = "exact"
    registry.store.upsert_participant(p)
    # Verify the alias spelling survived in the store.
    stored = registry.store.get_participant(p.id)
    assert stored.harness == alias, (
        f"expected alias {alias!r} in store, got {stored.harness!r}; "
        "registry.register normalized the harness and the test is hollow"
    )
    if not live:
        registry.mark_dead(p.id)
    return p


async def test_alias_resolves_at_validate_resume_identity(registry, monkeypatch, fake_tmux):
    """Point 6: an alias-stored harness row resolves at _validate_resume_identity.

    Mutation: revert ``normalize_harness(participant.harness) == canonical`` to
    ``participant.harness == req.harness`` in ``_validate_resume_identity``.
    This test dies with ``BadRequest: no trusted dead ... binding`` because
    the alias-stored row is not found by raw comparison.
    """
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    _alias_predecessor(registry, alias="claude-code", canonical="claude", session_id="sess-alias-1")
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
        await spawner.spawn(req)
    except BadRequest as exc:
        if "no trusted" in str(exc):
            pytest.fail("alias-stored row was not found at _validate_resume_identity: " + str(exc))
        # Any other BadRequest means identity validation passed and a later
        # gate refused — the canonical filter worked.


async def test_alias_resolves_at_resume_state_peer_scan(registry, monkeypatch):
    """Point 6: an alias-stored harness row resolves at the methods.py
    _resume_state peer scan.

    Mutation: revert ``normalize(other.harness) == normalize(p.harness)`` to
    ``other.harness == p.harness`` in the ``_resume_state`` peer loop.
    This test dies with ``state == "resumable"`` instead of
    ``"owned_by_live"`` because the alias-stored live peer is not found.
    """
    from theater.daemon.methods import _resume_state

    # A live peer stored under alias "claude-code" should be found when
    # checking a dead row stored under canonical "claude".
    _alias_predecessor(
        registry,
        alias="claude-code",
        canonical="claude",
        session_id="sess-peer-1",
        live=True,
        pane="%99",
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


async def test_alias_resolves_at_resolve_resume_reference(registry, monkeypatch, fake_tmux):
    """Point 6: an alias-stored harness row resolves at _resolve_resume_reference
    (resume=<participant-id>).

    Mutation: revert
    ``normalize_harness(participant.harness) != normalize_harness(req.harness)``
    to ``participant.harness != req.harness`` in ``_resolve_resume_reference``.
    This test dies with ``BadRequest: belongs to harness`` because the
    alias-stored row's harness does not match the request by raw comparison.
    """
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    p = _alias_predecessor(
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
        await spawner.spawn(req)
    except BadRequest as exc:
        if "belongs to harness" in str(exc):
            pytest.fail("alias-stored row was rejected at _resolve_resume_reference: " + str(exc))
        # Any other BadRequest means the reference resolved and a later gate
        # refused — the canonical filter worked.


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

    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
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
