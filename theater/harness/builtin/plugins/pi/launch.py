"""Pi launch and isolated-session resume planning."""

from __future__ import annotations

import json
from pathlib import Path

from theater import paths
from theater.harness.contracts.callbacks import LaunchContext, ResumeContext
from theater.harness.contracts.launch import LaunchPlan, ResumeLaunchOverlay, theater_binary
from theater.models import BadRequest

from .constants import PI_ISOLATION_MARKER, PI_SESSIONS_DIRNAME
from .isolation import canonical, marker_text, validate_domain


def participant_root(participant_id: str) -> Path:
    """The Theater-owned Pi session directory for one participant."""
    return paths.observation_dir("pi", participant_id) / PI_SESSIONS_DIRNAME


def plan_launch(context: LaunchContext) -> LaunchPlan:
    """Give Pi an exact session id and a launch-local Theater MCP endpoint."""
    session_id = context.resume or context.participant_id
    session_dir = participant_root(context.participant_id)
    config = {
        "mcpServers": {
            "theater": {
                "command": theater_binary(),
                "args": ["mcp", "--id", context.participant_id, "--harness", "pi"],
            }
        }
    }
    argv = [
        "pi",
        "--session-id",
        session_id,
        "--theater-mcp-config",
        str(context.config_path),
    ]
    files = {context.config_path: json.dumps(config, indent=2) + "\n"}
    env: dict[str, str] = {}
    if context.resume is None:
        argv[3:3] = ["--session-dir", str(session_dir)]
        files[session_dir / PI_ISOLATION_MARKER] = marker_text(
            participant_id=context.participant_id,
            transcript_domain=session_dir,
        )
    else:
        # The resume overlay replaces this isolated fallback with the trusted
        # predecessor domain. Pi's own environment is only consulted because
        # a resumed invocation deliberately omits --session-dir.
        env["PI_CODING_AGENT_SESSION_DIR"] = str(session_dir)
    if context.model:
        argv += ["--model", context.model]
    if context.reasoning_effort:
        argv += ["--thinking", context.reasoning_effort]
    if context.prompt:
        argv.append(context.prompt)
    return LaunchPlan(
        argv=argv,
        env=env,
        files=files,
        session_id=session_id,
        transcript_domain=str(canonical(session_dir)),
    )


def resume_launch_overlay(context: ResumeContext) -> ResumeLaunchOverlay:
    """Reuse only an authenticated predecessor Pi session directory."""
    predecessor = context.predecessor
    if predecessor.session_id is None:
        raise BadRequest("cannot resume Pi session safely: predecessor has no native session id")
    if predecessor.transcript_domain is None:
        raise BadRequest(
            "cannot resume Pi session safely: predecessor has no isolated transcript domain. "
            "Rebind or migrate the session into a Theater isolated Pi domain, then retry."
        )
    domain = canonical(Path(predecessor.transcript_domain))
    marker = validate_domain(domain)
    if marker is None:
        raise BadRequest(
            "cannot resume Pi session safely: predecessor uses a legacy or untrusted transcript "
            "domain. Rebind or migrate it into a Theater isolated Pi domain, then retry."
        )
    owner_id = marker.get("participant_id")
    if not isinstance(owner_id, str) or not _domain_owner_is_trusted(
        owner_id=owner_id,
        domain=domain,
        trusted_owners=context.trusted_session_owners,
    ):
        raise BadRequest(
            "cannot resume Pi session safely: isolated transcript domain belongs to a different "
            "Theater session lineage. Rebind or migrate it into its own isolated Pi domain, "
            "then retry."
        )
    if predecessor.transcript_location is not None:
        try:
            Path(predecessor.transcript_location).resolve().relative_to(domain)
        except (OSError, ValueError) as exc:
            raise BadRequest(
                "cannot resume Pi session safely: predecessor transcript location is outside its "
                "isolated transcript domain"
            ) from exc
    return ResumeLaunchOverlay(
        env={"PI_CODING_AGENT_SESSION_DIR": str(domain)},
        transcript_domain=str(domain),
        cwd=predecessor.cwd,
    )


def _domain_owner_is_trusted(*, owner_id: str, domain: Path, trusted_owners: tuple) -> bool:
    return any(
        participant.id == owner_id
        and participant.transcript_domain is not None
        and canonical(Path(participant.transcript_domain)) == domain
        for participant in trusted_owners
    )
