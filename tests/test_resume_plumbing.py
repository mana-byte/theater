"""Tests for the resume plumbing in ``theater.harness``.

The resume parameter follows the same compat contract as model: it is
forwarded into the plugin only when non-None, so a third-party adapter
written against the old signature keeps working. A resume asked of a
harness whose ``plan_launch`` has no ``resume`` parameter is refused by
name, not as a TypeError from inside the plugin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theater.harness import (
    HARNESSES,
    Harness,
    LaunchPlan,
    check_resume,
    plan_launch,
    supports_model,
    supports_reasoning,
    supports_resume,
)
from theater.harness.contracts.harness import LaunchParameterSupport
from theater.models import BadRequest


class _ResumeHarness(Harness):
    """A harness that accepts resume, for testing the forwarding path."""

    name = "resume-test"
    binary = "resume-test"
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
        return LaunchPlan(argv=["resume-test", participant_id])


class _NoResumeHarness(Harness):
    """A harness whose plan_launch predates the resume parameter."""

    name = "no-resume-test"
    binary = "no-resume-test"
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
        return LaunchPlan(argv=["no-resume-test", participant_id])


@pytest.fixture
def resume_harness(monkeypatch):
    h = _ResumeHarness()
    monkeypatch.setitem(HARNESSES, "resume-test", h)
    return h


@pytest.fixture
def no_resume_harness(monkeypatch):
    h = _NoResumeHarness()
    monkeypatch.setitem(HARNESSES, "no-resume-test", h)
    return h


def test_supports_resume_reads_the_signature(resume_harness):
    assert supports_resume(resume_harness) is True


def test_supports_resume_false_when_parameter_absent(no_resume_harness):
    assert supports_resume(no_resume_harness) is False


def test_explicit_launch_parameter_support_overrides_legacy_signature(no_resume_harness):
    no_resume_harness.launch_parameter_support = LaunchParameterSupport(
        model=True,
        reasoning_effort=True,
        resume=True,
    )

    assert supports_model(no_resume_harness) is True
    assert supports_reasoning(no_resume_harness) is True
    assert supports_resume(no_resume_harness) is True


def test_explicit_launch_parameter_support_can_disable_signature_parameters(resume_harness):
    resume_harness.launch_parameter_support = LaunchParameterSupport()

    assert supports_model(resume_harness) is False
    assert supports_reasoning(resume_harness) is False
    assert supports_resume(resume_harness) is False


def test_check_resume_refuses_a_harness_without_the_parameter(no_resume_harness):
    with pytest.raises(BadRequest, match="does not support resume"):
        check_resume("no-resume-test", "some-session-id")


def test_plan_launch_forwards_resume_only_when_non_none(resume_harness, tmp_path):
    """The compat contract: resume is forwarded as a keyword only when set.

    Identical to how model is forwarded. A None resume means 'no preference',
    and the plugin is never called with the keyword — so an adapter written
    before resume existed keeps working for every launch that does not name
    a session.
    """
    plan_launch(
        "resume-test",
        participant_id="abc",
        prompt="hello",
        config_path=tmp_path / "x.json",
        approval="manual",
    )
    assert resume_harness.seen_resume is None

    plan_launch(
        "resume-test",
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
            "no-resume-test",
            participant_id="abc",
            prompt="hello",
            config_path=tmp_path / "x.json",
            approval="manual",
            resume="sess-123",
        )


def test_plan_launch_without_resume_works_for_a_harness_without_it(no_resume_harness, tmp_path):
    """A harness that predates resume still launches when no resume is asked."""
    plan = plan_launch(
        "no-resume-test",
        participant_id="abc",
        prompt="hello",
        config_path=tmp_path / "x.json",
        approval="manual",
    )
    assert plan.argv == ["no-resume-test", "abc"]
