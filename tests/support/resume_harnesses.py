"""Fake resume-capable harnesses and trusted predecessors for the resume spawn tests."""

from __future__ import annotations

from pathlib import Path

from theater.harness import Harness, LaunchPlan
from theater.harness.base import ResumeLaunchOverlay
from theater.harness.contracts.harness import LaunchParameterSupport
from theater.harness.observation import TranscriptObserver


class _NullObserver(TranscriptObserver):
    has_transcript = True

    def find_transcript(self, *, cwd, session_id=None, after=None):
        return None

    def session_id(self, transcript):
        return None

    def parse(self, line, index, *, clip_text=True):
        return []

    def is_idle_screen(self, capture):
        return False


class ResumeHarness(Harness):
    """A harness that accepts resume and delivers the prompt on the command line."""

    name = "resume-spawn-test"
    binary = "resume-spawn-test"
    icon = "R"
    launch_parameter_support = LaunchParameterSupport(model=True, resume=True)

    def __init__(self):
        self.observer = _NullObserver()

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

    def resume_launch_overlay(self, *, predecessor, trusted_session_owners):
        # Test harness: accepts any predecessor domain unconditionally.
        return ResumeLaunchOverlay()


class NoResumeHarness(Harness):
    """A harness whose plan_launch predates the resume parameter."""

    name = "no-resume-spawn-test"
    binary = "no-resume-spawn-test"
    icon = "N"

    def __init__(self):
        self.observer = _NullObserver()

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
    ) -> LaunchPlan:
        return LaunchPlan(argv=["no-resume-spawn-test", participant_id])


class DropsPromptHarness(ResumeHarness):
    """A harness that accepts resume but silently drops the prompt (opencode)."""

    name = "drops-prompt-test"
    binary = "drops-prompt-test"
    icon = "D"
    resume_takes_prompt = False


def trusted_resume(
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


def trusted_predecessor(
    registry,
    *,
    harness: str,
    session_id: str = "sess-abc",
    transcript_domain: str | None = None,
):
    """A dead participant whose session id is trusted, optionally in a transcript domain."""
    p = registry.register(harness=harness, pane=None, cwd="/tmp", session_id=session_id)
    p.session_correlation = "exact"
    if transcript_domain is not None:
        p.transcript_domain = transcript_domain
    registry.store.upsert_participant(p)
    registry.mark_dead(p.id)
    return registry.get(p.id)
