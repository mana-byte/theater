"""Pi launch planning: isolated argv, approval modes, bootstrap and literal prompts."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.launch_preparation import prepare_provider_reservation
from tests.support.pi_transcripts import append, session_record
from theater import paths
from theater.daemon.spawner import Spawner, SpawnRequest
from theater.harness import theater_mcp_servers
from theater.harness.builtin.plugins.pi import bootstrap, launch
from theater.harness.builtin.plugins.pi.constants import (
    PI_ISOLATION_MARKER,
)
from theater.harness.builtin.plugins.pi.launch import (
    participant_root,
    plan_launch,
    resume_launch_overlay,
)
from theater.harness.builtin.plugins.pi.manifest import MANIFEST
from theater.harness.contracts.callbacks import LaunchContext, ResumeContext
from theater.harness.contracts.runtime import RuntimeCompatibility
from theater.harness.manifests.compiler import compile_manifest
from theater.models import BadRequest, Participant

pytestmark = pytest.mark.usefixtures("available_harness_binaries")


def test_pi_launch_advertises_only_yolo_and_builds_isolated_argv(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    config_path = tmp_path / "mcp.json"

    plan = compile_manifest("pi", MANIFEST).plan_launch(
        participant_id="pi-child",
        prompt="inspect this",
        config_path=config_path,
        approval="yolo",
        model="openai/gpt-5.6",
        reasoning_effort="high",
        mcp_servers=theater_mcp_servers("pi-child", "pi"),
    )

    session_dir = paths.participant_observation_dir("pi-child", "pi") / "sessions"
    assert plan.argv[:3] == [
        sys.executable,
        "-m",
        "theater.harness.builtin.plugins.pi.bootstrap",
    ]
    assert plan.argv[plan.argv.index("--session-dir") + 1] == str(session_dir)
    assert plan.argv[plan.argv.index("--theater-mcp-config") + 1] == str(config_path)
    assert plan.argv[plan.argv.index("--model") + 1] == "openai/gpt-5.6"
    assert plan.argv[plan.argv.index("--thinking") + 1] == "high"
    assert plan.argv[-1] == "inspect this"
    assert plan.session_id == "pi-child"
    assert plan.transcript_domain == str(session_dir.resolve())
    assert set(plan.files) == {config_path, session_dir / PI_ISOLATION_MARKER}
    config = json.loads(plan.files[config_path])
    assert config["mcpServers"]["theater"]["args"] == [
        "mcp",
        "--id",
        "pi-child",
        "--harness",
        "pi",
        "--toolset",
        "control",
    ]
    # Pi has no Theater-enforced permission prompt or sandbox, so the launch
    # contract must advertise only the behavior plan_launch produces.
    assert MANIFEST.launch.approvals == ("yolo",)
    assert plan.env["PI_OFFLINE"] == "1"
    assert plan.env["PI_CACHE_RETENTION"] == "long"
    assert plan.env["PI_SKIP_VERSION_CHECK"] == "1"
    assert plan.env["PI_TELEMETRY"] == "0"


def test_pi_launch_argv_is_independent_of_approval_and_manifest_rejects_other_modes(
    tmp_path, monkeypatch
) -> None:
    # plan_launch never reads context.approval, so the produced argv must be
    # identical for every approval value. The manifest gate is the only thing
    # that enforces the contract, and it must reject manual/edits because Pi
    # has no Theater-enforced permission prompt or sandbox for them.
    from theater.harness.manifests.compiler import compile_manifest

    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    config_path = tmp_path / "mcp.json"
    base = plan_launch(
        LaunchContext(
            participant_id="pi-child",
            prompt="inspect this",
            config_path=config_path,
            approval="yolo",
        )
    ).argv
    for approval in ("manual", "edits", "yolo"):
        plan = plan_launch(
            LaunchContext(
                participant_id="pi-child",
                prompt="inspect this",
                config_path=config_path,
                approval=approval,
            )
        )
        assert plan.argv == base

    harness = compile_manifest("pi", MANIFEST)
    for rejected in ("manual", "edits"):
        with pytest.raises(BadRequest, match="approval must be one of yolo"):
            harness.plan_launch(
                participant_id="pi-child",
                prompt="inspect this",
                config_path=config_path,
                approval=rejected,
            )


@pytest.mark.parametrize("cold", [True, False])
def test_bootstrap_executes_the_binary_verified_by_launch_admission(tmp_path, monkeypatch, cold):
    binary = str(tmp_path / "pi-qualified")

    def probe(context):
        assert context.binary == binary
        return RuntimeCompatibility(supported=True, policy="simulated-launch")

    monkeypatch.setattr(launch, "probe_pi_frontend_compatibility", probe)
    plan = plan_launch(
        LaunchContext(
            "pi-child",
            "hello",
            tmp_path / "mcp.json",
            "yolo",
            binary=binary,
            resume=None if cold else "old.jsonl",
        )
    )
    captured = {}

    def execvpe(file, args, env):
        captured.update(file=file, args=args, env=env)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(sys, "argv", ["bootstrap", *plan.argv[3:]])
    monkeypatch.setattr(bootstrap.os, "execvpe", execvpe)
    with pytest.raises(RuntimeError, match="exec intercepted"):
        bootstrap.main()
    assert captured["file"] == binary
    assert captured["args"][0] == binary
    assert "--theater-binary" not in captured["args"]
    assert "--theater-cold-session-id" not in captured["args"]


async def test_pi_resume_spawn_forks_to_fresh_native_identity_and_domain(
    tmp_path, monkeypatch, registry
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    workdir = tmp_path / "work"
    workdir.mkdir()
    predecessor = registry.register(
        harness="pi", pane=None, cwd=str(workdir), session_id="old-native-id"
    )
    predecessor.session_correlation = "exact"
    domain = participant_root(predecessor.id)
    cold_plan = plan_launch(LaunchContext(predecessor.id, "", tmp_path / "old-config.json", "yolo"))
    domain.mkdir(parents=True)
    marker = domain / PI_ISOLATION_MARKER
    marker.write_text(cold_plan.files[marker], encoding="utf-8")
    transcript = domain / "old.jsonl"
    append(transcript, session_record(session_id="old-native-id", cwd=workdir))
    predecessor.transcript_domain = str(domain.resolve())
    predecessor.transcript_location = str(transcript.resolve())
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    reservation = await prepare_provider_reservation(
        Spawner(registry),
        SpawnRequest(
            harness="pi",
            prompt="continue",
            cwd=str(tmp_path / "ignored-cwd"),
            approval="yolo",
            resume="old-native-id",
        ),
    )

    successor = reservation.participant
    command = reservation.plan.argv
    successor_domain = participant_root(successor.id)
    assert command[command.index("--session-id") + 1] == successor.id
    assert command[command.index("--session-dir") + 1] == str(successor_domain)
    assert command[command.index("--fork") + 1] == str(transcript.resolve())
    assert successor.session_id == successor.id
    assert successor.transcript_domain == str(successor_domain.resolve())
    assert successor.resumed_from_id == predecessor.id
    assert successor.cwd == str(workdir)
    assert successor_domain != domain


def _resumable_predecessor(tmp_path, monkeypatch) -> tuple[Participant, Path]:
    """A dead predecessor whose isolated domain passes every resume check."""
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    cold_plan = plan_launch(LaunchContext("pi-old", "", tmp_path / "config.json", "yolo"))
    domain = Path(cold_plan.transcript_domain or "")
    domain.mkdir(parents=True)
    marker = domain / PI_ISOLATION_MARKER
    marker.write_text(cold_plan.files[marker], encoding="utf-8")
    transcript = domain / "old.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    return (
        Participant(
            id="pi-old",
            harness="pi",
            cwd=str(tmp_path),
            session_id="old-native-id",
            transcript_domain=str(domain),
            transcript_location=str(transcript),
        ),
        transcript,
    )


# Each refusal drives one BadRequest branch of resume_launch_overlay.
REFUSALS = (
    "no-native-session-id",
    "no-transcript-domain",
    "untrusted-domain",
    "foreign-lineage",
    "no-transcript-location",
    "transcript-outside-domain",
    "missing-transcript",
    "transcript-symlink",
)


@pytest.mark.parametrize("refusal", REFUSALS)
def test_pi_resume_refuses_unsafe_predecessors(tmp_path, monkeypatch, refusal: str) -> None:
    predecessor, transcript = _resumable_predecessor(tmp_path, monkeypatch)
    domain = Path(predecessor.transcript_domain or "")
    trusted: tuple[Participant, ...] = (predecessor,)
    if refusal == "no-native-session-id":
        predecessor = replace(predecessor, session_id=None)
    elif refusal == "no-transcript-domain":
        predecessor = replace(predecessor, transcript_domain=None)
    elif refusal == "untrusted-domain":
        bare = tmp_path / "bare"
        bare.mkdir()
        predecessor = replace(predecessor, transcript_domain=str(bare))
    elif refusal == "foreign-lineage":
        trusted = ()
    elif refusal == "no-transcript-location":
        predecessor = replace(predecessor, transcript_location=None)
    elif refusal == "transcript-outside-domain":
        outside = tmp_path / "outside.jsonl"
        outside.write_text("{}\n", encoding="utf-8")
        predecessor = replace(predecessor, transcript_location=str(outside))
    elif refusal == "missing-transcript":
        transcript.unlink()
    elif refusal == "transcript-symlink":
        linked = domain / "linked.jsonl"
        linked.symlink_to(transcript)
        predecessor = replace(predecessor, transcript_location=str(linked))
    with pytest.raises(BadRequest, match="cannot resume Pi session safely"):
        resume_launch_overlay(
            ResumeContext(predecessor=predecessor, trusted_session_owners=trusted)
        )


# Pi's parseArgs routes by argv prefix (`--help`, `-h`, `@file`, unknown `--` flags),
# so launch prepends a newline: only the start of each element is inspected and a
# leading newline lands the whole prompt in `messages` verbatim.
HOSTILE_PROMPTS = [
    "- item: finish the migration",
    "@teammate please review the diff",
]


def _plan_argv(prompt: str) -> list[str]:
    plan = plan_launch(
        LaunchContext(
            participant_id="pi-child",
            prompt=prompt,
            config_path=Path("mcp.json"),
            approval="yolo",
        )
    )
    return plan.argv


@pytest.mark.parametrize("prompt", HOSTILE_PROMPTS)
def test_pi_launch_prefixes_hostile_prompts_with_a_leading_newline(prompt: str) -> None:
    assert _plan_argv(prompt)[-1] == f"\n{prompt}"
