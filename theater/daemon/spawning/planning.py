"""Launch-plan construction, receipt pre-flight, identity recording, plan-file writes.

These run during ``reserve`` after the participant and any requested worktree
exist.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import replace
from pathlib import Path

from theater import paths
from theater.daemon.artifacts import artifacts_for_plan
from theater.daemon.plugins.attachments import (
    PlannedMcpSidecar,
    emit_registry_diagnostic_omissions,
    merge_sidecars,
    omit_conflicting_sidecars,
    omit_unrenderable_sidecars,
    plan_sidecars,
    revoke_sidecars,
    sidecar_specs,
)
from theater.daemon.spawning.models import SpawnRequest
from theater.harness import get as get_harness
from theater.harness import plan_launch, supports_mcp_rendering, theater_mcp_servers
from theater.harness.base import LaunchPlan, ResumeLaunchOverlay, theater_binary
from theater.harness.contracts.callbacks import (
    HookInstallContext,
    HookInstallOverlay,
    OtelInstallContext,
    OtelInstallOverlay,
)
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.launch import ChannelCredential
from theater.harness.contracts.manifest import HookChannelManifest, OtelChannelManifest
from theater.mcp_plugins import McpServerSpec
from theater.models import BadRequest, Participant
from theater.provenance import TranscriptProvenance

_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")

__all__ = [
    "build_plan",
    "install_hook_plan",
    "install_otel_plan",
    "record_launch_identity",
    "record_plan_artifacts",
    "validate_receipt_plan",
    "write_plan_files",
]


def build_plan(
    req: SpawnRequest,
    participant: Participant,
    overlay: ResumeLaunchOverlay | None,
    *,
    registry=None,
) -> LaunchPlan:
    """Construct the launch plan, including best-effort configured sidecars."""
    sidecars: tuple[PlannedMcpSidecar, ...] = ()
    if registry is not None:
        emit_registry_diagnostic_omissions(participant, store=registry.store)
        if supports_mcp_rendering(req.harness):
            sidecars = plan_sidecars(
                participant,
                cwd=participant.cwd or req.cwd,
                store=registry.store,
            )
        else:
            omit_unrenderable_sidecars(participant, store=registry.store)

    try:
        plan = _harness_plan(
            req, participant, overlay, mcp_servers=_mcp_servers(req, participant, sidecars)
        )
    except Exception as exc:
        if registry is None or not sidecars:
            raise
        try:
            plan = _harness_plan(
                req, participant, overlay, mcp_servers=_mcp_servers(req, participant, ())
            )
        except Exception:
            raise exc from None
        revoke_sidecars(
            sidecars,
            participant=participant,
            store=registry.store,
            reason=(
                f"the harness launch funnel rejected MCP sidecars: {type(exc).__name__}: {exc}"
            ),
        )
        sidecars = ()

    if registry is not None and sidecars:
        accepted = omit_conflicting_sidecars(
            sidecars,
            plan,
            participant=participant,
            store=registry.store,
        )
        if accepted != sidecars:
            sidecars = accepted
            plan = _harness_plan(
                req,
                participant,
                overlay,
                mcp_servers=_mcp_servers(req, participant, sidecars),
            )
            accepted = omit_conflicting_sidecars(
                sidecars,
                plan,
                participant=participant,
                store=registry.store,
            )
            if accepted != sidecars:
                sidecars = accepted
                plan = _harness_plan(
                    req,
                    participant,
                    overlay,
                    mcp_servers=_mcp_servers(req, participant, sidecars),
                )
        plan = merge_sidecars(plan, sidecars)
    return plan


def _mcp_servers(
    req: SpawnRequest,
    participant: Participant,
    sidecars: tuple[PlannedMcpSidecar, ...],
) -> tuple[McpServerSpec, ...]:
    """Compose Theater's control endpoints with successful sidecars."""
    return (*theater_mcp_servers(participant.id, req.harness), *sidecar_specs(sidecars))


def _harness_plan(
    req: SpawnRequest,
    participant: Participant,
    overlay: ResumeLaunchOverlay | None,
    *,
    mcp_servers: tuple[McpServerSpec, ...],
) -> LaunchPlan:
    """Call the harness funnel against its frozen sidecar keyword interface."""
    config_path = paths.mcp_config_path(participant.id)
    resume_reference = req.resume
    if overlay is not None and overlay.resume_reference is not None:
        resume_reference = overlay.resume_reference
    plan = plan_launch(
        req.harness,
        participant_id=participant.id,
        prompt=req.prompt,
        config_path=config_path,
        approval=req.approval,
        model=req.model,
        reasoning_effort=req.reasoning_effort,
        resume=resume_reference,
        mcp_servers=mcp_servers,
    )
    return _merge_overlay(plan, overlay)


def _merge_overlay(plan: LaunchPlan, overlay: ResumeLaunchOverlay | None) -> LaunchPlan:
    """Apply the generic resume overlay after either harness funnel path."""
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
    if plan.channel_credentials:
        raise BadRequest(
            "launch plan sets channel_credentials; core owns native channel credentials and mints "
            "them only through declared channel installers"
        )
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
    reserved.update(credential.token_path for credential in plan.channel_credentials)
    if plan.receipt_token_path is not None:
        reserved.add(plan.receipt_token_path)
    credentials: list[ChannelCredential] = []
    for channel in channels:
        if not channel.bindings or channel.installer is None:
            continue
        token_path = paths.observation_dir(participant.harness, participant.id) / (
            f"hook-{channel.declaration.id}.token"
        )
        _validate_channel_token_path(token_path, participant, reserved)
        credential = ChannelCredential(
            kind=ChannelKind.HOOK,
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
        _merge_channel_overlay(
            env=env,
            files=files,
            reserved=reserved,
            overlay_env=overlay.env,
            overlay_files=overlay.files,
            participant=participant,
            kind=ChannelKind.HOOK,
        )
        reserved.add(token_path)
        credentials.append(credential)
    if not credentials:
        return plan
    return replace(
        plan,
        env=env,
        files=files,
        channel_credentials=plan.channel_credentials + tuple(credentials),
    )


def install_otel_plan(plan: LaunchPlan, participant: Participant, observer, runtime) -> LaunchPlan:
    """Mint credentials and merge safe launch-local native OTel overlays."""
    if runtime is None or not runtime.available:
        return plan
    channels = tuple(
        manifest
        for manifest in observer.enrichment_manifests()
        if isinstance(manifest, OtelChannelManifest)
        and manifest.unavailable_reason is None
        and manifest.bindings
        and manifest.installer is not None
    )
    if not channels:
        return plan
    env = dict(plan.env)
    files = dict(plan.files)
    reserved = set(files) | set(plan.private_files)
    reserved.update(credential.token_path for credential in plan.channel_credentials)
    if plan.receipt_token_path is not None:
        reserved.add(plan.receipt_token_path)
    credentials: list[ChannelCredential] = []
    for channel in channels:
        correlation = channel.correlation
        if correlation is None:
            raise BadRequest("native OTel channel lacks exact correlation fields")
        installer = channel.installer
        if installer is None:
            continue
        token_path = paths.observation_dir(participant.harness, participant.id) / (
            f"otel-{channel.declaration.id}.token"
        )
        _validate_channel_token_path(token_path, participant, reserved)
        credential = ChannelCredential(
            kind=ChannelKind.OTEL,
            channel_id=channel.declaration.id,
            token=secrets.token_urlsafe(32),
            token_path=token_path,
        )
        overlay = installer(
            OtelInstallContext(
                participant_id=participant.id,
                harness=participant.harness,
                channel_id=credential.channel_id,
                token_file=token_path,
                endpoint=runtime.endpoint,
                auth_header=correlation.auth_header,
                resource_attributes={
                    correlation.participant_attribute: participant.id,
                    correlation.harness_attribute: participant.harness,
                    correlation.channel_attribute: credential.channel_id,
                },
            )
        )
        if not isinstance(overlay, OtelInstallOverlay):
            raise BadRequest("native OTel installer must return an OtelInstallOverlay")
        header_env = overlay.credential_header_env
        if not isinstance(header_env, str) or not _ENVIRONMENT_NAME.fullmatch(header_env):
            raise BadRequest(
                "native OTel installer must declare a credential_header_env using an environment "
                "variable name"
            )
        _reject_inherited_otel_environment(overlay, header_env)
        _merge_channel_overlay(
            env=env,
            files=files,
            reserved=reserved,
            overlay_env=overlay.env,
            overlay_files=overlay.files,
            participant=participant,
            kind=ChannelKind.OTEL,
        )
        if header_env in env:
            raise BadRequest(
                f"native OTel credential_header_env collides with {header_env!r} in the launch plan"
            )
        env[header_env] = f"{correlation.auth_header}={credential.token}"
        reserved.add(token_path)
        credentials.append(credential)
    if not credentials:
        return plan
    return replace(
        plan,
        env=env,
        files=files,
        channel_credentials=plan.channel_credentials + tuple(credentials),
    )


def _reject_inherited_otel_environment(overlay: OtelInstallOverlay, header_env: str) -> None:
    for key in overlay.env:
        if isinstance(key, str) and key in os.environ:
            raise BadRequest(
                f"native OTel installer environment collides with inherited environment {key!r}"
            )
    if header_env in os.environ:
        raise BadRequest(
            f"native OTel credential_header_env collides with inherited environment {header_env!r}"
        )


def _validate_channel_token_path(path, participant: Participant, reserved: set) -> None:
    if path.is_symlink():
        raise BadRequest("native channel token path is a symlink")
    obs_dir = paths.observation_dir(participant.harness, participant.id)
    try:
        path.resolve(strict=False).relative_to(obs_dir.resolve(strict=False))
    except ValueError:
        raise BadRequest(
            "native channel token path must resolve under the harness observation directory"
        ) from None
    if any(existing.resolve(strict=False) == path.resolve(strict=False) for existing in reserved):
        raise BadRequest("native channel token path collides with a launch-plan file")


def _merge_channel_overlay(
    *,
    env: dict,
    files: dict,
    reserved: set,
    overlay_env,
    overlay_files,
    participant,
    kind: ChannelKind,
) -> None:
    obs_dir = paths.observation_dir(participant.harness, participant.id).resolve(strict=False)
    label = "hook" if kind is ChannelKind.HOOK else "native OTel"
    for key, value in overlay_env.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise BadRequest(f"{label} installer environment must map non-blank strings to strings")
        if key in env:
            raise BadRequest(f"{label} installer environment collides with {key!r}")
        env[key] = value
    for path, contents in overlay_files.items():
        if not isinstance(path, Path) or not isinstance(contents, str):
            raise BadRequest(f"{label} installer files must map Paths to strings")
        if path.is_symlink():
            raise BadRequest(f"{label} installer file is a symlink")
        try:
            path.resolve(strict=False).relative_to(obs_dir)
        except ValueError:
            raise BadRequest(
                f"{label} installer files must resolve under the harness observation directory"
            ) from None
        if any(
            existing.resolve(strict=False) == path.resolve(strict=False) for existing in reserved
        ):
            raise BadRequest(f"{label} installer file collides with a launch-plan file")
        files[path] = contents
        reserved.add(path)


def record_launch_identity(
    participant: Participant,
    plan: LaunchPlan,
    registry,
    *,
    runtime=None,
    observer=None,
) -> None:
    """Persist exact launch facts before the process can write output."""
    if (
        plan.session_id is None
        and plan.transcript_domain is None
        and plan.receipt_token is None
        and not plan.channel_credentials
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
    for credential in plan.channel_credentials:
        registry.store.set_channel_credential(
            participant.id,
            harness=participant.harness,
            kind=credential.kind,
            channel_id=credential.channel_id,
            token=credential.token,
            token_path=str(credential.token_path),
        )
    if runtime is not None and observer is not None:
        channels = {
            channel.declaration.id: channel
            for channel in observer.enrichment_manifests()
            if isinstance(channel, OtelChannelManifest)
        }
        for credential in plan.channel_credentials:
            if credential.kind is not ChannelKind.OTEL:
                continue
            channel = channels.get(credential.channel_id)
            if channel is not None:
                runtime.activate(
                    participant_id=participant.id,
                    harness=participant.harness,
                    channel=channel,
                    credential=credential,
                )


def record_plan_artifacts(participant: Participant, plan: LaunchPlan, registry) -> None:
    """Persist ownership before any launch-plan file is written."""
    registry.store.add_participant_artifacts(
        participant.id,
        artifacts_for_plan(plan, participant),
    )


def write_plan_files(plan: LaunchPlan) -> None:
    """Write public config files, private launch secrets, and the receipt token."""
    for path, contents in plan.files.items():
        _write_launch_file(path, contents, private=False)
    for path, contents in plan.private_files.items():
        _write_launch_file(path, contents, private=True)
    # Core owns the receipt token file; the plugin must NOT also put it in private_files.
    if plan.receipt_token_path is not None and plan.receipt_token is not None:
        _write_launch_file(plan.receipt_token_path, plan.receipt_token + "\n", private=True)
    for credential in plan.channel_credentials:
        _write_launch_file(credential.token_path, credential.token + "\n", private=True)


def _write_launch_file(path: Path, contents: str, *, private: bool) -> None:
    """Write one prevalidated artifact without following a final-path symlink."""
    if path.is_symlink() or path.parent.is_symlink():
        raise BadRequest(f"launch artifact path {path!r} contains a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise BadRequest(f"launch artifact parent {path.parent!r} is a symlink")
    if private:
        path.parent.chmod(0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600 if private else 0o666)
    if private:
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(contents)
