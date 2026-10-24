"""Pi launch planning: isolated argv, approval modes, bootstrap and literal prompts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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
)
from theater.harness.builtin.plugins.pi.manifest import MANIFEST
from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.runtime import RuntimeCompatibility
from theater.harness.manifests.compiler import compile_manifest

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
    from theater.models import BadRequest

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


@pytest.mark.requires_node
def test_pi_bootstrap_suppresses_only_the_expected_cold_warning(monkeypatch) -> None:
    node = shutil.which("node") or "node"
    captured: dict[str, object] = {}

    def execvpe(file, args, env):
        captured.update(file=file, args=args, env=env)
        raise RuntimeError("exec intercepted")

    monkeypatch.setenv("NODE_OPTIONS", "--no-warnings")
    monkeypatch.setattr(sys, "argv", ["bootstrap", "--theater-cold-session-id", "child-1"])
    monkeypatch.setattr(bootstrap.os, "execvpe", execvpe)
    with pytest.raises(RuntimeError, match="exec intercepted"):
        bootstrap.main()

    env = captured["env"]
    assert isinstance(env, dict)
    assert str(env["NODE_OPTIONS"]).startswith("--no-warnings ")
    result = subprocess.run(
        [
            node,
            "-e",
            "console.error('other'); "
            "console.error(\"\\x1b[33mWarning: No project session found with id 'child-1'; "
            'creating a new session with that id.\\x1b[39m"); '
            "console.error('after')",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stderr.splitlines() == ["other", "after"]


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


def test_pi_runtime_routes_only_the_proven_native_controls() -> None:
    from theater.daemon.controls.routing import manifest_control_routes
    from theater.harness.contracts.runtime import ControlTransport, RuntimeCapability

    runtime = MANIFEST.runtime
    assert runtime is not None
    channel = runtime.channel
    # The durable Pi transcript stays completion authority: the initial CLI
    # prompt has no control operation, so live ownership would strand it.
    assert channel.drives_job_completion is False
    assert runtime.legacy_fallback == frozenset()
    routes = manifest_control_routes(runtime)
    assert routes["interrupt"] == ControlTransport.NATIVE_RUNTIME.value
    assert routes["steer"] is None
    assert runtime.unavailable_capabilities == frozenset({RuntimeCapability.STEER})


# Pi's parseArgs (packages/coding-agent/src/cli/args.ts) routes by argv prefix:
# lines ~91 (--help/-h), ~225 ("@" becomes a file arg, even after "--" per
# lines 82-88), ~227 (unknown "--" flag), ~241 ("Unknown option" for "-").
# Only the START of each element is inspected, so a leading newline lands the
# whole prompt in `messages`; buildInitialMessage then uses it verbatim.
_PI_SOURCE = Path(os.environ.get("PI_SOURCE_DIR", "/Users/manaiki.laut/Desktop/coding_clis/pi"))


HOSTILE_PROMPTS = [
    "- item: finish the migration",
    "---",
    "--help",
    "-h",
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


def _pi_argv(plan_argv: list[str]) -> list[str]:
    """The argv bootstrap hands to `pi`: it strips its own cold-session pair."""
    args = plan_argv[3:]  # drop the python -m bootstrap prefix
    assert args[:2] == ["--theater-cold-session-id", "pi-child"]
    return args[2:]


def _parse_with_pi(pi_argv: list[str]) -> dict[str, object]:
    """Run Pi's real parseArgs; skip when the source checkout is missing."""
    args_ts = _PI_SOURCE / "packages/coding-agent/src/cli/args.ts"
    node = shutil.which("node") or "node"
    if not args_ts.is_file():
        pytest.skip("the Pi source checkout is unavailable")
    # Bare imports in args.ts (chalk, cross-spawn) are uninstalled here and
    # unused by parseArgs, so unresolved bare specifiers get a stub module.
    probe = (
        "import { registerHooks } from 'node:module';\n"
        "const stub = 'data:text/javascript,"
        "export default new Proxy(function(){},{get:(t,k)=>()=>({})});';\n"
        "registerHooks({\n"
        "  resolve(specifier, context, nextResolve) {\n"
        "    try { return nextResolve(specifier, context); }\n"
        "    catch { return { shortCircuit: true, url: stub }; }\n"
        "  },\n"
        "});\n"
        f"const {{ parseArgs }} = await import({json.dumps(str(args_ts))});\n"
        "const r = parseArgs(JSON.parse(process.argv[2]));\n"
        "console.log(JSON.stringify({\n"
        "  messages: r.messages, fileArgs: r.fileArgs, help: r.help === true,\n"
        "  errors: r.diagnostics.filter((d) => d.type === 'error'),\n"
        "}));\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "probe.mjs"
        script.write_text(probe)
        result = subprocess.run(
            [node, "--experimental-strip-types", str(script), json.dumps(pi_argv)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    if result.returncode != 0:
        pytest.fail(f"pi parseArgs probe failed:\n{result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.requires_node
@pytest.mark.parametrize("prompt", HOSTILE_PROMPTS)
def test_pi_launch_prompt_survives_pi_parser(prompt: str) -> None:
    planned = _plan_argv(prompt)
    assert planned[-1] == f"\n{prompt}"
    parsed = _parse_with_pi(_pi_argv(planned))
    assert parsed == {
        "messages": [f"\n{prompt}"],
        "fileArgs": [],
        "help": False,
        "errors": [],
    }


@pytest.mark.requires_node
def test_pi_launch_ordinary_prompt_is_unchanged() -> None:
    planned = _plan_argv("inspect this repository")
    assert planned[-1] == "inspect this repository"
    parsed = _parse_with_pi(_pi_argv(planned))
    assert parsed["messages"] == ["inspect this repository"]
