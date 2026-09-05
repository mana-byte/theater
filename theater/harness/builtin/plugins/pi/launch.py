"""Pi launch and isolated-session resume planning."""

from __future__ import annotations

import sys
from pathlib import Path

from theater import paths
from theater.harness.contracts.callbacks import LaunchContext, ResumeContext
from theater.harness.contracts.launch import LaunchPlan, ResumeLaunchOverlay
from theater.models import BadRequest

from .constants import PI_ISOLATION_MARKER, PI_LAUNCH_ENV, PI_SESSIONS_DIRNAME
from .isolation import canonical, marker_text, validate_domain

_THEATER_MCP_BRIDGE = Path(__file__).with_name("theater_mcp_bridge.ts")


def participant_root(participant_id: str) -> Path:
    """The Theater-owned Pi session directory for one participant."""
    return paths.participant_observation_dir(participant_id, "pi") / PI_SESSIONS_DIRNAME


def plan_launch(context: LaunchContext) -> LaunchPlan:
    """Give Pi an isolated session id and its MCP bridge launch arguments."""
    session_id = context.participant_id
    session_dir = participant_root(context.participant_id)
    argv = [
        sys.executable,
        "-m",
        "theater.harness.builtin.plugins.pi.bootstrap",
    ]
    if context.resume is None:
        argv += ["--theater-cold-session-id", session_id]
    argv += [
        "--extension",
        str(_THEATER_MCP_BRIDGE),
        "--session-id",
        session_id,
        "--session-dir",
        str(session_dir),
    ]
    if context.resume is not None:
        # The resume overlay replaces the native id with the authenticated
        # predecessor transcript path. Pi copies it into this participant's
        # new isolated session rather than continuing the predecessor file.
        argv += ["--fork", context.resume]
    argv += [
        "--theater-mcp-config",
        str(context.config_path),
    ]
    files = {}
    env: dict[str, str] = dict(PI_LAUNCH_ENV)
    files[session_dir / PI_ISOLATION_MARKER] = marker_text(
        participant_id=context.participant_id,
        transcript_domain=session_dir,
    )
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
    """Fork only an authenticated predecessor Pi transcript."""
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
    if predecessor.transcript_location is None:
        raise BadRequest(
            "cannot resume Pi session safely: predecessor has no attached transcript to fork"
        )
    location = Path(predecessor.transcript_location)
    try:
        resolved_location = location.resolve(strict=True)
        resolved_location.relative_to(domain)
    except (OSError, ValueError) as exc:
        raise BadRequest(
            "cannot resume Pi session safely: predecessor transcript location is missing or "
            "outside its isolated transcript domain"
        ) from exc
    if location.is_symlink() or not resolved_location.is_file():
        raise BadRequest(
            "cannot resume Pi session safely: predecessor transcript location is not a regular file"
        )
    return ResumeLaunchOverlay(
        cwd=predecessor.cwd,
        resume_reference=str(resolved_location),
    )


def _domain_owner_is_trusted(*, owner_id: str, domain: Path, trusted_owners: tuple) -> bool:
    return any(
        participant.id == owner_id
        and participant.transcript_domain is not None
        and canonical(Path(participant.transcript_domain)) == domain
        for participant in trusted_owners
    )
