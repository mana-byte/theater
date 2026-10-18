"""`spawn_session(resume=...)` against a real daemon: refusals by state, concurrent claims."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from theater.client import DaemonClient
from theater.harness import HARNESSES, Harness, LaunchPlan
from theater.harness.contracts.harness import LaunchParameterSupport
from theater.harness.observation import TranscriptObserver
from theater.protocol import RemoteError


class _ResumeObs(TranscriptObserver):
    """Minimal observer for test harnesses."""

    has_transcript = True

    def find_transcript(self, *, cwd, session_id=None, after=None):
        return None

    def session_id(self, transcript):
        return None

    def parse(self, line, index, *, clip_text=True):
        return []

    def is_idle_screen(self, capture):
        return False


class _PinResumeHarness(Harness):
    """Harness that supports resume — used by most resume_state cases."""

    name = "resume-pin-test"
    binary = "resume-pin-test"
    icon = "P"
    launch_parameter_support = LaunchParameterSupport(resume=True)

    def __init__(self):
        self.observer = _ResumeObs()

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        resume: str | None = None,
    ) -> LaunchPlan:
        argv = ["resume-pin-test"]
        if resume:
            argv += ["--resume", resume]
        return LaunchPlan(argv=argv)


class _PinNoResumeHarness(Harness):
    """Harness whose plan_launch predates the resume parameter."""

    name = "no-resume-pin-test"
    binary = "no-resume-pin-test"
    icon = "Q"

    def __init__(self):
        self.observer = _ResumeObs()

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
    ) -> LaunchPlan:
        return LaunchPlan(argv=["no-resume-pin-test"])


@pytest.fixture
def pin_harnesses(monkeypatch, mcp_daemon, terminal_provider):
    """Install the two test harnesses and stub shutil.which."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(
        "theater.daemon.spawning.provider_launch.shutil.which", lambda b: f"/usr/bin/{b}"
    )
    monkeypatch.setitem(HARNESSES, "resume-pin-test", _PinResumeHarness())
    monkeypatch.setitem(HARNESSES, "no-resume-pin-test", _PinNoResumeHarness())
    terminal_provider.install(mcp_daemon)


async def test_resume_state_live_spawn_refuses(mcp_daemon, pin_harnesses):
    """live => spawn(resume=id) refuses with _resolve_resume_reference's 'still live' message."""
    async with DaemonClient(autostart=False) as c:
        me = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        pid = me["id"]
        rows = await c.call("participants.list")
        row = next(r for r in rows if r["id"] == pid)
        assert row["resume_state"] == "live"
        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume=pid,
            )
        assert exc.value.code == "bad_request"
        assert "still live" in exc.value.message


async def test_resume_state_no_session_id_spawn_refuses(mcp_daemon, pin_harnesses):
    """no_session_id => spawn(resume=id) refuses with 'not recorded' message."""
    async with DaemonClient(autostart=False) as c:
        me = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        pid = me["id"]
        mcp_daemon.registry.mark_dead(pid)
        rows = await c.call("participants.list", include_dead=True)
        row = next(r for r in rows if r["id"] == pid)
        assert row["resume_state"] == "no_session_id"
        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume=pid,
            )
        assert exc.value.code == "bad_request"
        assert "harness session id" in exc.value.message


async def test_resume_state_harness_cannot_resume_spawn_refuses(mcp_daemon, pin_harnesses):
    """harness_cannot_resume => spawn(resume=session_id) refuses with 'does not support' message."""
    async with DaemonClient(autostart=False) as c:
        p = await c.call("hello", harness="no-resume-pin-test", cwd="/tmp")
        pid = p["id"]
        part = mcp_daemon.registry.get(pid)
        part.session_id = "sess-noresume"
        part.session_correlation = "operator"
        mcp_daemon.store.upsert_participant(part)
        mcp_daemon.registry.mark_dead(pid)
        rows = await c.call("participants.list", include_dead=True)
        row = next(r for r in rows if r["id"] == pid)
        assert row["resume_state"] == "harness_cannot_resume"
        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="no-resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume="sess-noresume",
            )
        assert exc.value.code == "bad_request"
        assert "does not support resume" in exc.value.message


async def test_resume_state_untrusted_spawn_refuses(mcp_daemon, pin_harnesses):
    """untrusted => spawn(resume=session_id) refuses with 'no trusted ... binding' message."""
    async with DaemonClient(autostart=False) as c:
        p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        pid = p["id"]
        part = mcp_daemon.registry.get(pid)
        part.session_id = "sess-untrusted"
        part.session_correlation = "heuristic"
        mcp_daemon.store.upsert_participant(part)
        mcp_daemon.registry.mark_dead(pid)
        rows = await c.call("participants.list", include_dead=True)
        row = next(r for r in rows if r["id"] == pid)
        assert row["resume_state"] == "untrusted"
        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume="sess-untrusted",
            )
        assert exc.value.code == "bad_request"
        # The spawner reaches the "no trusted dead binding" gate, not the live-owner gate.
        assert "no trusted" in exc.value.message


async def test_resume_state_resumable_spawn_succeeds(mcp_daemon, pin_harnesses):
    """resumable => spawn(resume=session_id) succeeds (no refusal)."""
    async with DaemonClient(autostart=False) as c:
        p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        pid = p["id"]
        part = mcp_daemon.registry.get(pid)
        part.session_id = "sess-resumable"
        part.session_correlation = "operator"
        mcp_daemon.store.upsert_participant(part)
        mcp_daemon.registry.mark_dead(pid)
        rows = await c.call("participants.list", include_dead=True)
        row = next(r for r in rows if r["id"] == pid)
        assert row["resume_state"] == "resumable"
        result = await c.call(
            "spawn",
            harness="resume-pin-test",
            prompt="",
            approval="manual",
            cwd="/tmp",
            resume="sess-resumable",
        )
        assert result["id"] is not None


async def test_resume_predecessor_claim_prevents_a_second_live_recovery(mcp_daemon, pin_harnesses):
    async with DaemonClient(autostart=False) as c:
        predecessor = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        participant = mcp_daemon.registry.get(predecessor["id"])
        participant.session_id = "sess-predecessor-claim"
        participant.session_correlation = "operator"
        mcp_daemon.store.upsert_participant(participant)
        mcp_daemon.registry.mark_dead(predecessor["id"])

        first = await c.call(
            "spawn",
            harness="resume-pin-test",
            prompt="",
            approval="manual",
            cwd="/tmp",
            resume=predecessor["id"],
        )
        assert mcp_daemon.registry.get(first["id"]).resumed_from_id == predecessor["id"]
        rows = await c.call("participants.list", include_dead=True)
        row = next(row for row in rows if row["id"] == predecessor["id"])
        assert row["resume_state"] == "owned_by_live"

        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume=predecessor["id"],
            )
        assert exc.value.code == "bad_request"
        assert "live successor" in exc.value.message

        mcp_daemon.registry.mark_dead(first["id"])
        recovered = await c.call(
            "spawn",
            harness="resume-pin-test",
            prompt="",
            approval="manual",
            cwd="/tmp",
            resume=predecessor["id"],
        )
        assert recovered["id"] != first["id"]


async def test_concurrent_resume_claims_leave_one_live_successor(mcp_daemon, pin_harnesses):
    async with DaemonClient(autostart=False) as setup:
        predecessor = await setup.call("hello", harness="resume-pin-test", cwd="/tmp")
        participant = mcp_daemon.registry.get(predecessor["id"])
        participant.session_id = "sess-concurrent-claim"
        participant.session_correlation = "operator"
        mcp_daemon.store.upsert_participant(participant)
        mcp_daemon.registry.mark_dead(predecessor["id"])

    async with DaemonClient(autostart=False) as first, DaemonClient(autostart=False) as second:
        results = await asyncio.gather(
            first.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume=predecessor["id"],
            ),
            second.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume=predecessor["id"],
            ),
            return_exceptions=True,
        )

    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, RemoteError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "bad_request"
    assert "live successor" in failures[0].message


async def test_resume_state_owned_by_live_trusted_dead_spawn_refuses(mcp_daemon, pin_harnesses):
    """owned_by_live, trusted dead + trusted live => 'trusted owner is still live' message."""
    async with DaemonClient(autostart=False) as c:
        # Live owner with trusted binding.
        live_p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        lo_id = live_p["id"]
        live_part = mcp_daemon.registry.get(lo_id)
        live_part.session_id = "sess-owned"
        live_part.session_correlation = "operator"
        mcp_daemon.store.upsert_participant(live_part)

        # Dead predecessor sharing the same session id (trusted provenance).
        dead_p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        dp_id = dead_p["id"]
        dead_part = mcp_daemon.registry.get(dp_id)
        dead_part.session_id = "sess-owned"
        dead_part.session_correlation = "operator"
        mcp_daemon.store.upsert_participant(dead_part)
        mcp_daemon.registry.mark_dead(dp_id)

        rows = await c.call("participants.list", include_dead=True)
        dp_row = next(r for r in rows if r["id"] == dp_id)
        assert dp_row["resume_state"] == "owned_by_live"

        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume="sess-owned",
            )
        assert exc.value.code == "bad_request"
        assert "still live" in exc.value.message
        assert lo_id in exc.value.message


async def test_resume_state_owned_by_live_untrusted_dead_spawn_refuses(mcp_daemon, pin_harnesses):
    """owned_by_live wins even when the subject dead row is untrusted.

    An untrusted dead row and a trusted live peer sharing a session id: the
    spawner's _validate_resume_identity filters to trusted rows only, so the
    untrusted dead row enters neither list and the live trusted peer triggers
    the 'trusted owner is still live' gate.  resume_state must report
    owned_by_live (not untrusted), and the spawner must refuse with the
    'still live' message — proving the two implementations agree.
    """
    async with DaemonClient(autostart=False) as c:
        # Live owner with trusted binding.
        live_p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        lo_id = live_p["id"]
        live_part = mcp_daemon.registry.get(lo_id)
        live_part.session_id = "sess-mixed"
        live_part.session_correlation = "operator"
        mcp_daemon.store.upsert_participant(live_part)

        # Dead row sharing the session id but with UNTRUSTED provenance.
        dead_p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        dp_id = dead_p["id"]
        dead_part = mcp_daemon.registry.get(dp_id)
        dead_part.session_id = "sess-mixed"
        dead_part.session_correlation = "heuristic"  # untrusted
        mcp_daemon.store.upsert_participant(dead_part)
        mcp_daemon.registry.mark_dead(dp_id)

        rows = await c.call("participants.list", include_dead=True)
        dp_row = next(r for r in rows if r["id"] == dp_id)
        # Must be owned_by_live, not untrusted — the live peer dominates.
        assert dp_row["resume_state"] == "owned_by_live"

        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume="sess-mixed",
            )
        assert exc.value.code == "bad_request"
        # Must hit the live-owner gate, not the untrusted gate.
        assert "still live" in exc.value.message
        assert lo_id in exc.value.message
