"""GC cleanup of participant-owned artifacts: secrets, launch files and orphan roots."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.builders import make_participant
from tests.support.gc_sweep import count_rows, retention
from theater import paths
from theater.constants.observation import FENCED_TURNS
from theater.daemon import artifacts as artifacts_module
from theater.daemon import gc as gc_module
from theater.daemon.artifacts import (
    ArtifactKind,
    OwnedArtifact,
    artifacts_for_plan,
    cleanup_orphan_paths,
    remove_secret_file,
)
from theater.daemon.gc import sweep
from theater.daemon.registry import Registry
from theater.daemon.schema import participant_artifacts, participants
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.launch import LaunchPlan
from theater.models import BadRequest, Participant, Status, Tier


def _materialize_participant_artifacts(store, participant: Participant) -> tuple[Path, ...]:
    root = paths.participant_dir(participant.id)
    observation = paths.participant_observation_dir(participant.id, participant.harness)
    files = (
        paths.mcp_config_path(participant.id),
        paths.participant_launch_dir(participant.id) / "harness.json",
        observation / "messages.jsonl",
        observation / "receipt-token",
        observation / "hook.token",
        paths.participant_plugin_dir(participant.id, "alerts") / "credential",
    )
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name)
    store.add_participant_artifacts(
        participant.id,
        (
            OwnedArtifact(root, ArtifactKind.DIRECTORY),
            *(OwnedArtifact(path, ArtifactKind.FILE) for path in files),
        ),
    )
    store.set_receipt_token(
        participant.id,
        "receipt-secret",
        token_path=str(observation / "receipt-token"),
    )
    store.set_channel_credential(
        participant.id,
        harness=participant.harness,
        kind=ChannelKind.HOOK,
        channel_id="native-hooks",
        token="channel-secret",
        token_path=str(observation / "hook.token"),
    )
    return (root, *files)


def test_read_only_artifact_trees_are_removed_without_leaving_the_owner_root(
    theater_home, tmp_path
):
    """Vibe's plugin packages are r-x directories; rmtree alone failed on them forever."""
    owner = "a" * 12
    root = paths.participant_dir(owner)
    package = root / "observations" / "vibe" / "plugins" / "packages" / "82" / "hash"
    package.mkdir(parents=True)
    (package / "module.py").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (package / "link").symlink_to(outside)
    for directory in (package, package.parent, package.parent.parent):
        directory.chmod(0o555)
    outside.chmod(0o500)  # a followed symlink would try to delete from here

    try:
        failures = cleanup_orphan_paths(((OwnedArtifact(root, ArtifactKind.DIRECTORY), owner),))
        assert failures == () and not root.exists()
        assert (outside / "keep.txt").read_text() == "keep"
        assert outside.stat().st_mode & 0o777 == 0o500  # never chmodded through the link
    finally:
        outside.chmod(0o700)


async def test_invalid_artifact_owner_does_not_starve_later_participants(store):
    blocked_id = "000000000001"
    later_id = "000000000002"
    make_participant(store, pid=blocked_id)
    store.conn.execute(
        participant_artifacts.insert().values(
            participant_id=blocked_id,
            # Match the repository's canonical key even when /tmp is a symlink
            # (as on macOS), so baseline insertion cannot hide the corrupt row.
            path=str(paths.mcp_config_path(blocked_id).resolve(strict=False)),
            kind="corrupt",
        )
    )
    make_participant(store, pid=later_id)

    result = await sweep(store, retention(batch=1))

    assert result.participants == 1
    assert store.get_participant(blocked_id) is not None
    assert store.get_participant(later_id) is None
    assert store.participant_artifact_owner_ids() == (blocked_id,)
    with pytest.raises(ValueError):
        store.participant_artifacts(blocked_id)


async def test_retained_dead_participant_keeps_nonsecret_artifacts(store):
    participant = make_participant(store, pid="abcdef123456", status=Status.IDLE)
    artifact_paths = _materialize_participant_artifacts(store, participant)
    Registry(store).mark_dead(participant.id)
    observation = paths.participant_observation_dir(participant.id, participant.harness)
    assert not (observation / "receipt-token").exists()
    assert not (observation / "hook.token").exists()
    make_participant(store, pid="child", parent_id=participant.id, status=Status.IDLE)

    result = await sweep(store, retention())

    assert result.participants == 0
    assert store.get_participant(participant.id) is not None
    assert paths.participant_dir(participant.id).exists()
    assert all(
        path.exists() for path in artifact_paths if path.name not in {"receipt-token", "hook.token"}
    )


async def test_retained_dead_row_cleans_secret_credentials_after_crash(store):
    participant = make_participant(store, pid="abcdef123456")
    _materialize_participant_artifacts(store, participant)
    store.set_status(participant.id, Status.DEAD)
    make_participant(store, pid="child", parent_id=participant.id, status=Status.IDLE)

    observation = paths.participant_observation_dir(participant.id, participant.harness)
    receipt = observation / "receipt-token"
    channel = observation / "hook.token"
    assert receipt.exists() and channel.exists()

    result = await sweep(store, retention())

    assert result.participants == 0
    assert store.get_participant(participant.id) is not None
    assert not receipt.exists() and not channel.exists()
    assert store.get_meta(f"receipt_token:{participant.id}") is None
    assert store.get_meta(f"channel_credential:{participant.id}:hook:native-hooks") is None
    assert paths.participant_dir(participant.id).exists()


async def test_eligible_participant_removes_all_owned_artifacts(store):
    participant = make_participant(store, pid="abcdef123456")
    artifact_paths = _materialize_participant_artifacts(store, participant)
    global_state = paths.plugin_state_dir("alerts")
    global_state.mkdir()
    (global_state / "state.db").write_text("keep")
    with store.write_unit() as unit:  # turn fences are bounded, newest kept, and go with it
        for index in range(FENCED_TURNS + 2):
            fence = f"turn-{index:03d}"
            store.turn_fences.add(
                participant.id,
                [fence],
                job_handle=None,
                created_at=index,
                connection=unit.connection,
            )
    assert store.turn_fences.keys(participant.id)[0] == "turn-002"
    assert len(store.turn_fences.keys(participant.id)) == FENCED_TURNS

    result = await sweep(store, retention())

    assert result.participants == 1
    assert store.turn_fences.keys(participant.id) == ()
    assert store.get_participant(participant.id) is None
    assert not paths.participant_dir(participant.id).exists()
    assert all(not path.exists() for path in artifact_paths)
    assert (global_state / "state.db").exists()
    assert store.get_meta(f"receipt_token:{participant.id}") is None
    assert store.participant_artifact_owner_ids() == ()


def test_launch_artifact_paths_are_owned_and_follow_runtime_home(
    theater_home, monkeypatch, tmp_path
):
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setenv("THEATER_HOME", str(runtime_home))
    paths.ensure_home()
    participant = Participant(id="abcdef123456", harness="custom", tier=Tier.SPAWNED)

    valid = LaunchPlan(files={paths.mcp_config_path(participant.id): "{}"}, argv=[])
    owned = artifacts_for_plan(valid, participant)
    assert paths.mcp_config_path(participant.id) in {artifact.path for artifact in owned}

    outside = tmp_path / "user-file"
    with pytest.raises(BadRequest):
        artifacts_for_plan(LaunchPlan(files={outside: "unsafe"}, argv=[]), participant)

    other = paths.mcp_config_path("fedcba654321")
    with pytest.raises(BadRequest):
        artifacts_for_plan(LaunchPlan(files={other: "unsafe"}, argv=[]), participant)


def test_secret_cleanup_uses_runtime_home_and_refuses_symlink_parents(monkeypatch, tmp_path):
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setenv("THEATER_HOME", str(runtime_home))
    paths.ensure_home()
    participant_id = "abcdef123456"
    legitimate = paths.participant_observation_dir(participant_id, "custom") / "receipt-token"
    legitimate.parent.mkdir(parents=True, exist_ok=True)
    legitimate.write_text("secret")

    remove_secret_file(legitimate, owner_id=participant_id)

    assert not legitimate.exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "token"
    target.write_text("secret")
    linked_parent = paths.participant_dir(participant_id) / "linked"
    linked_parent.parent.mkdir(parents=True, exist_ok=True)
    linked_parent.symlink_to(outside, target_is_directory=True)

    remove_secret_file(linked_parent / "token", owner_id=participant_id)

    assert target.exists()


async def test_secret_cleanup_refuses_external_paths_but_removes_metadata(store, tmp_path):
    receipt = tmp_path / "receipt-token"
    channel = tmp_path / "channel-token"
    receipt.write_text("secret")
    channel.write_text("secret")
    store.set_receipt_token("missing", "receipt", token_path=str(receipt))
    store.set_channel_credential(
        "missing",
        harness="custom",
        kind=ChannelKind.HOOK,
        channel_id="hooks",
        token="channel",
        token_path=str(channel),
    )

    await sweep(store, retention())

    assert receipt.exists() and channel.exists()
    assert store.get_meta("receipt_token:missing") is None
    assert store.get_meta("channel_credential:missing:hook:hooks") is None


async def test_failed_artifact_cleanup_keeps_ownership_for_retry(store, monkeypatch):
    participant = make_participant(store, pid="abcdef123456")
    _materialize_participant_artifacts(store, participant)
    target = paths.participant_dir(participant.id).resolve(strict=False)
    original_remove = artifacts_module._remove_one
    failed = False

    def fail_once(artifact):
        nonlocal failed
        if artifact.path == target and not failed:
            failed = True
            raise OSError("temporarily busy")
        return original_remove(artifact)

    monkeypatch.setattr(artifacts_module, "_remove_one", fail_once)
    first = await sweep(store, retention())

    assert first.participants == 1
    assert store.get_participant(participant.id) is None
    assert target.exists()
    assert store.participant_artifacts(participant.id)

    monkeypatch.setattr(artifacts_module, "_remove_one", original_remove)
    second = await sweep(store, retention())

    assert second.participants == 0
    assert not target.exists()
    assert store.participant_artifact_owner_ids() == ()


async def test_orphan_participant_roots_are_cleaned_without_touching_user_data(store):
    owner_id = "fedcba654321"
    orphan = paths.participant_dir(owner_id)
    (orphan / "observations" / "vibe").mkdir(parents=True)
    (orphan / "observations" / "vibe" / "messages.jsonl").write_text("durable")
    unrelated = paths.plugins_dir() / "keep"
    unrelated.mkdir()
    (unrelated / "manifest.py").write_text("keep")

    await sweep(store, retention())

    assert not orphan.exists()
    assert unrelated.exists()


async def test_orphan_cleanup_rechecks_owner_before_removal(store, monkeypatch):
    owner_id = "fedcba654321"
    orphan = paths.participant_dir(owner_id)
    orphan.mkdir(parents=True)
    (orphan / "data").write_text("keep")
    original_to_thread = gc_module.workers.to_thread

    async def race_to_thread(fn, /, *args, label, **kwargs):
        result = await original_to_thread(fn, *args, label=label, **kwargs)
        if fn is gc_module.orphan_paths:
            make_participant(store, pid=owner_id, status=Status.IDLE)
        return result

    monkeypatch.setattr(gc_module.workers, "to_thread", race_to_thread)

    await sweep(store, retention())

    assert store.get_participant(owner_id) is not None
    assert orphan.exists()


async def test_participant_artifact_cleanup_honours_batches(store):
    for index in range(5):
        participant = make_participant(store, pid=f"{index + 1:012x}")
        _materialize_participant_artifacts(store, participant)

    result = await sweep(store, retention(batch=2))

    assert result.participants == 5
    assert count_rows(store, participants) == 0
    assert store.participant_artifact_owner_ids() == ()
