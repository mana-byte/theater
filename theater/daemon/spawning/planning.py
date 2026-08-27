"""Launch-plan construction, receipt pre-flight, identity recording, plan-file writes.

These run during ``reserve`` after the participant exists but before the
worktree is created — so a rejected plan leaves nothing behind.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import replace
from pathlib import Path

from theater import paths
from theater.daemon.spawning.models import SpawnRequest
from theater.harness import get as get_harness
from theater.harness import plan_launch
from theater.harness.base import LaunchPlan, ResumeLaunchOverlay, theater_binary
from theater.harness.contracts.callbacks import HookInstallContext, HookInstallOverlay
from theater.harness.contracts.launch import HookCredential
from theater.harness.contracts.manifest import HookChannelManifest
from theater.models import BadRequest, Participant
from theater.provenance import TranscriptProvenance

__all__ = [
    "build_plan",
    "install_hook_plan",
    "record_launch_identity",
    "validate_receipt_plan",
    "write_plan_files",
]


def build_plan(
    req: SpawnRequest,
    participant: Participant,
    overlay: ResumeLaunchOverlay | None,
) -> LaunchPlan:
    """Construct the launch plan and merge the resume overlay, if any."""
    config_path = paths.mcp_config_path(participant.id)
    plan = plan_launch(
        req.harness,
        participant_id=participant.id,
        prompt=req.prompt,
        config_path=config_path,
        approval=req.approval,
        model=req.model,
        reasoning_effort=req.reasoning_effort,
        resume=req.resume,
    )
    if overlay is None:
        return plan
    env = {**plan.env, **overlay.env}
    transcript_domain = plan.transcript_domain
    if overlay.transcript_domain is not None:
        transcript_domain = overlay.transcript_domain
    return replace(plan, env=env, transcript_domain=transcript_domain)


def validate_receipt_plan(plan: LaunchPlan, participant: Participant) -> str | None:
    """Pre-flight: validate a receipt plan and mint the token.

    Returns the minted token string, or ``None`` when the plan does not
    use receipts. Core owns the secret: the plugin sets only
    ``receipt_token_path``, and core mints the token here.
    """
    if plan.receipt_token_path is None:
        return None
    if plan.receipt_token is not None:
        raise BadRequest(
            "launch plan sets receipt_token; core owns the receipt secret "
            "and mints it from receipt_token_path. The plugin should set "
            "only receipt_token_path, not receipt_token."
        )
    # The observer must actually implement the hook.
    harness = get_harness(participant.harness)
    observer = getattr(harness, "observer", None)
    if observer is None:
        raise BadRequest(
            f"harness {participant.harness!r} has no observer; cannot use transcript receipts"
        )
    if not observer.supports_transcript_receipts:
        raise BadRequest(
            f"harness {participant.harness!r} observer does not implement "
            "validate_transcript_receipt; a plugin must implement this hook "
            "to use transcript receipts. See docs/harness-plugins.md"
        )
    # Refuse an existing symlink first: the writer uses O_TRUNC which follows symlinks.
    if plan.receipt_token_path.is_symlink():
        raise BadRequest(
            f"receipt_token_path {plan.receipt_token_path!r} is a symlink; "
            "refusing to write a token through a symlink"
        )
    # The receipt token path must live under the harness's observation dir.
    obs_dir = paths.observation_dir(participant.harness, participant.id)
    try:
        resolved_token = plan.receipt_token_path.resolve(strict=False)
        resolved_obs = obs_dir.resolve(strict=False)
        resolved_token.relative_to(resolved_obs)
    except ValueError:
        raise BadRequest(
            f"receipt_token_path {plan.receipt_token_path!r} must resolve "
            f"under the harness observation directory {obs_dir!r}"
        ) from None
    # No collision with public or private plan files.
    all_plan_paths = set(plan.files) | set(plan.private_files)
    for existing in all_plan_paths:
        if existing.resolve(strict=False) == resolved_token:
            raise BadRequest(
                f"receipt_token_path {plan.receipt_token_path!r} collides "
                f"with a launch-plan file {existing!r}"
            )
    return secrets.token_urlsafe(32)


def install_hook_plan(plan: LaunchPlan, participant: Participant, observer) -> LaunchPlan:
    """Mint credentials and merge launch-local hook installation overlays."""
    channels = tuple(
        manifest
        for manifest in observer.enrichment_manifests()
        if isinstance(manifest, HookChannelManifest)
    )
    if not channels:
        return plan
    env = dict(plan.env)
    files = dict(plan.files)
    reserved = set(files) | set(plan.private_files)
    if plan.receipt_token_path is not None:
        reserved.add(plan.receipt_token_path)
    credentials: list[HookCredential] = []
    for channel in channels:
        if not channel.bindings or channel.installer is None:
            continue
        token_path = paths.observation_dir(participant.harness, participant.id) / (
            f"hook-{channel.declaration.id}.token"
        )
        _validate_hook_token_path(token_path, participant, reserved)
        credential = HookCredential(
            channel_id=channel.declaration.id,
            token=secrets.token_urlsafe(32),
            token_path=token_path,
        )
        overlay = channel.installer(
            HookInstallContext(
                participant_id=participant.id,
                channel_id=credential.channel_id,
                token_file=token_path,
                theater_executable=theater_binary(),
            )
        )
        if not isinstance(overlay, HookInstallOverlay):
            raise BadRequest("hook installer must return a HookInstallOverlay")
        _merge_hook_overlay(
            env=env,
            files=files,
            reserved=reserved,
            overlay=overlay,
            participant=participant,
        )
        reserved.add(token_path)
        credentials.append(credential)
    if not credentials:
        return plan
    return replace(plan, env=env, files=files, hook_credentials=tuple(credentials))


def _validate_hook_token_path(path, participant: Participant, reserved: set) -> None:
    if path.is_symlink():
        raise BadRequest("hook token path is a symlink")
    obs_dir = paths.observation_dir(participant.harness, participant.id)
    try:
        path.resolve(strict=False).relative_to(obs_dir.resolve(strict=False))
    except ValueError:
        raise BadRequest(
            "hook token path must resolve under the harness observation directory"
        ) from None
    if any(existing.resolve(strict=False) == path.resolve(strict=False) for existing in reserved):
        raise BadRequest("hook token path collides with a launch-plan file")


def _merge_hook_overlay(*, env: dict, files: dict, reserved: set, overlay, participant) -> None:
    obs_dir = paths.observation_dir(participant.harness, participant.id).resolve(strict=False)
    for key, value in overlay.env.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise BadRequest("hook installer environment must map non-blank strings to strings")
        if key in env:
            raise BadRequest(f"hook installer environment collides with {key!r}")
        env[key] = value
    for path, contents in overlay.files.items():
        if not isinstance(path, Path) or not isinstance(contents, str):
            raise BadRequest("hook installer files must map Paths to strings")
        if path.is_symlink():
            raise BadRequest("hook installer file is a symlink")
        try:
            path.resolve(strict=False).relative_to(obs_dir)
        except ValueError:
            raise BadRequest(
                "hook installer files must resolve under the harness observation directory"
            ) from None
        if any(
            existing.resolve(strict=False) == path.resolve(strict=False) for existing in reserved
        ):
            raise BadRequest("hook installer file collides with a launch-plan file")
        files[path] = contents
        reserved.add(path)


def record_launch_identity(participant: Participant, plan: LaunchPlan, registry) -> None:
    """Persist exact launch facts before the process can write output."""
    if (
        plan.session_id is None
        and plan.transcript_domain is None
        and plan.receipt_token is None
        and not plan.hook_credentials
    ):
        return
    if plan.session_id is not None:
        participant.session_id = plan.session_id
        participant.session_correlation = str(TranscriptProvenance.EXACT)
    participant.transcript_domain = plan.transcript_domain
    registry.store.upsert_participant(participant)
    if plan.receipt_token is not None:
        token_path = plan.receipt_token_path
        registry.store.set_receipt_token(
            participant.id,
            plan.receipt_token,
            token_path=str(token_path) if token_path is not None else None,
        )
    for credential in plan.hook_credentials:
        registry.store.set_hook_credential(
            participant.id,
            harness=participant.harness,
            channel_id=credential.channel_id,
            token=credential.token,
            token_path=str(credential.token_path),
        )


def write_plan_files(plan: LaunchPlan) -> None:
    """Write public config files, private launch secrets, and the receipt token."""
    for path, contents in plan.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    for path, contents in plan.private_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(contents)
    # Core owns the receipt token file; the plugin must NOT also put it in private_files.
    if plan.receipt_token_path is not None and plan.receipt_token is not None:
        token_path = plan.receipt_token_path
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.parent.chmod(0o700)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(plan.receipt_token + "\n")
    for credential in plan.hook_credentials:
        token_path = credential.token_path
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.parent.chmod(0o700)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(credential.token + "\n")
