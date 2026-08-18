"""Tests for the generic transcript receipt mechanism.

These verify that a third-party (non-Claude) harness can use receipts end to
end, that the base default refuses, that pre-flight validation rejects bad
plans, and that core validates the returned candidate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from theater import paths
from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.daemon.spawner import Spawner
from theater.harness.base import Harness, LaunchPlan
from theater.harness.observation import HarnessObserver, TranscriptObserver
from theater.harness.source import TranscriptCandidate
from theater.models import BadRequest
from theater.protocol import RemoteError
from theater.transcript_identity import is_opaque_location

# -- A fake non-Claude harness that implements the receipt hook ---------------


class FakeObserver(TranscriptObserver):
    """A minimal observer whose validate_transcript_receipt hook works.

    Accepts a payload with ``session_id`` and ``path``, validates that the
    path is absolute and ends with ``.jsonl``, and returns a candidate. This
    is enough to drive a receipt end to end in a test.
    """

    def __init__(self, root: Path | None = None):
        self.root = root or Path("/tmp/fake-transcripts")

    def find_transcript(
        self, *, cwd: str, session_id: str | None = None, after: float | None = None
    ) -> Path | None:
        return None

    def session_id(self, transcript: Path) -> str | None:
        return transcript.stem or None

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list:
        return []

    def is_idle_screen(self, capture: str) -> bool:
        return False

    def validate_transcript_receipt(
        self,
        *,
        payload: Any,
        cwd: str | None,
        expected_session_id: str | None,
    ) -> TranscriptCandidate:
        session_id = payload.get("session_id")
        raw_path = payload.get("path")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("fake receipt payload missing session_id")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("fake receipt payload missing path")
        if is_opaque_location(raw_path):
            return TranscriptCandidate(location=raw_path, session_id=session_id)
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError("fake receipt path must be absolute")
        if path.suffix != ".jsonl":
            raise ValueError("fake receipt path must end with .jsonl")
        if path.stem != session_id:
            raise ValueError("fake receipt session_id does not match path stem")
        return TranscriptCandidate(location=str(path.resolve()), session_id=session_id)


class FakeHarness(Harness):
    name = "fake"
    binary = "fake"
    icon = "F"

    def __init__(self, root: Path | None = None):
        self.observer = FakeObserver(root=root)

    def plan_launch(self, *, participant_id, prompt, config_path, approval, **kwargs):
        token_path = paths.observation_dir("fake", participant_id) / "receipt-token"
        return LaunchPlan(
            argv=["fake", "--config", str(config_path)],
            files={config_path: "{}\n"},
            private_files={},
            receipt_token_path=token_path,
        )


# -- Helpers -------------------------------------------------------------------


def _spawn_fake(daemon, cwd: Path, *, pid: str, token: str = "tok"):
    participant = daemon.registry.create_spawned(harness="fake", cwd=str(cwd), pid=pid)
    daemon.store.set_receipt_token(participant.id, token)
    return participant


@pytest.fixture
async def fake_daemon(theater_home, tmp_path):
    root = tmp_path / "fake" / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    harness = FakeHarness(root=root)
    d = Daemon(harnesses={"fake": harness})
    await d.start()
    yield d, root
    await d.aclose()


@pytest.fixture
async def fake_client(fake_daemon):
    c = DaemonClient(autostart=False)
    await c.connect()
    yield c
    await c.aclose()


async def _fake_receipt(client, *, pid: str, token: str, session_id: str, path: str):
    return await client.call(
        "transcript.receipt",
        id=pid,
        token=token,
        payload={"session_id": session_id, "path": path},
    )


# -- 1. Fake harness end-to-end receipt ---------------------------------------


async def test_fake_harness_receipt_drives_end_to_end(fake_daemon, fake_client, tmp_path):
    daemon, root = fake_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_fake(daemon, cwd, pid="p-fake", token="secret")
    session = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    transcript = root / "project" / f"{session}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("")

    await _fake_receipt(
        client=fake_client,
        pid="p-fake",
        token="secret",
        session_id=session,
        path=str(transcript),
    )

    import asyncio

    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        p = daemon.store.get_participant("p-fake")
        if p.transcript_location == str(transcript.resolve()):
            break
        await asyncio.sleep(0.01)

    got = daemon.store.get_participant("p-fake")
    assert got.session_id == session
    assert got.transcript_location == str(transcript.resolve())


# -- 2. Base default refuses with a message naming the harness -----------------


def test_base_default_validate_transcript_receipt_refuses():
    class BareObserver(HarnessObserver):
        def is_idle_screen(self, capture: str) -> bool:
            return False

    bare = BareObserver()
    with pytest.raises(ValueError, match=r"BareObserver.*does not implement"):
        bare.validate_transcript_receipt(payload={}, cwd=None, expected_session_id=None)


# -- 3. Pre-flight rejects a plan with receipt_token_path against base default --


def test_preflight_rejects_receipt_plan_against_inheriting_observer(
    registry, tmp_path, monkeypatch
):

    class InheritingObserver(TranscriptObserver):
        def find_transcript(self, *, cwd, session_id=None, after=None):
            return None

        def session_id(self, transcript):
            return None

        def parse(self, line, index, *, clip_text=True):
            return []

        def is_idle_screen(self, capture):
            return False

    class InheritingHarness(Harness):
        name = "inheriting"
        binary = "fake"
        icon = "I"

        def __init__(self):
            self.observer = InheritingObserver()

        def plan_launch(self, *, participant_id, prompt, config_path, approval, **kwargs):
            return LaunchPlan(argv=["fake"])

    harness = InheritingHarness()
    monkeypatch.setattr("theater.daemon.spawner.get_harness", lambda name: harness)

    token_path = paths.observation_dir("inheriting", "p-x") / "receipt-token"
    plan = LaunchPlan(
        argv=["fake"],
        receipt_token_path=token_path,
    )
    participant = registry.create_spawned(harness="inheriting", cwd=str(tmp_path), pid="p-x")

    with pytest.raises(BadRequest, match="does not implement"):
        Spawner(registry)._validate_receipt_plan(plan, participant)


# -- 3b. Pre-flight refuses a plan that sets receipt_token (core owns the secret) --


def test_preflight_refuses_plan_that_sets_receipt_token(registry, tmp_path, monkeypatch):
    """Core mints the token; a plugin that sets receipt_token is refused."""
    harness = FakeHarness()
    monkeypatch.setattr("theater.daemon.spawner.get_harness", lambda name: harness)

    token_path = paths.observation_dir("fake", "p-x") / "receipt-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    plan = LaunchPlan(
        argv=["fake"],
        receipt_token="plugin-supplied-secret",
        receipt_token_path=token_path,
    )
    participant = registry.create_spawned(harness="fake", cwd=str(tmp_path), pid="p-x")

    with pytest.raises(BadRequest, match="core owns the receipt secret"):
        Spawner(registry)._validate_receipt_plan(plan, participant)


# -- 4. Receipt path validation: outside observation_dir, collision, symlink ----


def test_preflight_rejects_receipt_path_outside_observation_dir(registry, tmp_path, monkeypatch):
    harness = FakeHarness()
    monkeypatch.setattr("theater.daemon.spawner.get_harness", lambda name: harness)

    outside = tmp_path / "elsewhere" / "token"
    outside.parent.mkdir(parents=True, exist_ok=True)
    plan = LaunchPlan(
        argv=["fake"],
        receipt_token_path=outside,
    )
    participant = registry.create_spawned(harness="fake", cwd=str(tmp_path), pid="p-x")

    with pytest.raises(BadRequest, match="must resolve under"):
        Spawner(registry)._validate_receipt_plan(plan, participant)


def test_preflight_rejects_receipt_path_colliding_with_plan_files(registry, tmp_path, monkeypatch):
    harness = FakeHarness()
    monkeypatch.setattr("theater.daemon.spawner.get_harness", lambda name: harness)

    token_path = paths.observation_dir("fake", "p-x") / "receipt-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    plan = LaunchPlan(
        argv=["fake"],
        files={token_path: "config\n"},
        receipt_token_path=token_path,
    )
    participant = registry.create_spawned(harness="fake", cwd=str(tmp_path), pid="p-x")

    with pytest.raises(BadRequest, match="collides"):
        Spawner(registry)._validate_receipt_plan(plan, participant)


def test_preflight_rejects_existing_symlink_at_receipt_path(registry, tmp_path, monkeypatch):
    harness = FakeHarness()
    monkeypatch.setattr("theater.daemon.spawner.get_harness", lambda name: harness)

    token_path = paths.observation_dir("fake", "p-x") / "receipt-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "attacker"
    target.write_text("pwned")
    token_path.symlink_to(target)
    plan = LaunchPlan(
        argv=["fake"],
        receipt_token_path=token_path,
    )
    participant = registry.create_spawned(harness="fake", cwd=str(tmp_path), pid="p-x")

    with pytest.raises(BadRequest, match="symlink"):
        Spawner(registry)._validate_receipt_plan(plan, participant)


# -- 5. Core rejects bad candidates -------------------------------------------


async def test_core_rejects_candidate_with_empty_location(fake_daemon, fake_client, tmp_path):
    daemon, _root = fake_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_fake(daemon, cwd, pid="p-fake", token="secret")

    # Monkeypatch the observer to return a candidate with empty location
    def returning_empty(**kwargs):
        return TranscriptCandidate(location="", session_id="ok")

    daemon.observer.harnesses["fake"].observer.validate_transcript_receipt = returning_empty

    with pytest.raises(RemoteError, match="empty location"):
        await fake_client.call(
            "transcript.receipt",
            id="p-fake",
            token="secret",
            payload={"session_id": "ok", "path": "/tmp/x.jsonl"},
        )


async def test_core_rejects_candidate_with_missing_session_id(fake_daemon, fake_client, tmp_path):
    daemon, _root = fake_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_fake(daemon, cwd, pid="p-fake", token="secret")

    def returning_none(**kwargs):
        return TranscriptCandidate(location="/tmp/x.jsonl", session_id=None)

    daemon.observer.harnesses["fake"].observer.validate_transcript_receipt = returning_none

    with pytest.raises(RemoteError, match="empty session_id"):
        await fake_client.call(
            "transcript.receipt",
            id="p-fake",
            token="secret",
            payload={"session_id": "ok", "path": "/tmp/x.jsonl"},
        )


async def test_core_rejects_candidate_with_rejection_reason(fake_daemon, fake_client, tmp_path):
    daemon, _root = fake_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_fake(daemon, cwd, pid="p-fake", token="secret")

    def returning_rejected(**kwargs):
        return TranscriptCandidate(location="/tmp/x.jsonl", session_id="ok", rejection_reason="bad")

    daemon.observer.harnesses["fake"].observer.validate_transcript_receipt = returning_rejected

    with pytest.raises(RemoteError, match="rejection"):
        await fake_client.call(
            "transcript.receipt",
            id="p-fake",
            token="secret",
            payload={"session_id": "ok", "path": "/tmp/x.jsonl"},
        )


async def test_core_rejects_candidate_that_is_none(fake_daemon, fake_client, tmp_path):
    daemon, _root = fake_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_fake(daemon, cwd, pid="p-fake", token="secret")

    def returning_none(**kwargs):
        return None

    daemon.observer.harnesses["fake"].observer.validate_transcript_receipt = returning_none

    with pytest.raises(RemoteError, match="TranscriptCandidate"):
        await fake_client.call(
            "transcript.receipt",
            id="p-fake",
            token="secret",
            payload={"session_id": "ok", "path": "/tmp/x.jsonl"},
        )


async def test_core_rejects_candidate_that_is_not_a_candidate(fake_daemon, fake_client, tmp_path):
    daemon, _root = fake_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_fake(daemon, cwd, pid="p-fake", token="secret")

    def returning_dict(**kwargs):
        return {"location": "/tmp/x.jsonl", "session_id": "ok"}

    daemon.observer.harnesses["fake"].observer.validate_transcript_receipt = returning_dict

    with pytest.raises(RemoteError, match="TranscriptCandidate"):
        await fake_client.call(
            "transcript.receipt",
            id="p-fake",
            token="secret",
            payload={"session_id": "ok", "path": "/tmp/x.jsonl"},
        )


async def test_core_rejects_candidate_with_path_location(fake_daemon, fake_client, tmp_path):
    daemon, _root = fake_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_fake(daemon, cwd, pid="p-fake", token="secret")

    def returning_path_location(**kwargs):
        return TranscriptCandidate(location=Path("/tmp/x.jsonl"), session_id="ok")

    daemon.observer.harnesses["fake"].observer.validate_transcript_receipt = returning_path_location

    with pytest.raises(RemoteError, match="empty location"):
        await fake_client.call(
            "transcript.receipt",
            id="p-fake",
            token="secret",
            payload={"session_id": "ok", "path": "/tmp/x.jsonl"},
        )


# -- 6. Token deleted on death ------------------------------------------------


def test_receipt_token_deleted_on_death(registry, tmp_path):
    token_path = paths.observation_dir("fake", "p-fake") / "receipt-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("secret")

    p = registry.create_spawned(harness="fake", cwd=str(tmp_path), pid="p-fake")
    registry.store.set_receipt_token(p.id, "secret", token_path=str(token_path))

    assert token_path.exists()

    registry.mark_dead(p.id)

    assert not token_path.exists()
    assert registry.store.get_meta(f"receipt_token:{p.id}") is None


# -- 7. Re-receipt of already-owned opaque location with a live sibling -------


async def test_rereceipt_of_already_owned_location_allows_with_sibling(
    fake_daemon, fake_client, tmp_path
):
    """A re-receipt of a location the participant already owns is allowed
    even when a live same-cwd sibling exists.

    The allow/early-return branch in `_reject_unbound_same_cwd_receipt` must
    use `_same_location` (not raw `==`) so a path location stored in
    canonical form is reconciled with a receipt that names the same path
    through a different string. Without the fix, the raw `==` fails because
    the strings differ and the receipt is wrongly refused with
    "shares its cwd".
    """
    daemon, root = fake_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_fake(daemon, cwd, pid="first", token="one")
    _spawn_fake(daemon, cwd, pid="second", token="two")

    # Create a transcript reachable through two different path strings:
    # the canonical form (stored by the daemon) and a symlinked form
    # (what a hook might name). Both resolve to the same file.
    session = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    real_project = root / "real-project"
    real_project.mkdir(parents=True, exist_ok=True)
    transcript = real_project / f"{session}.jsonl"
    transcript.write_text("")
    # Symlink "project" -> "real-project" so the hook names a different path
    # that resolves to the same file.
    link_project = root / "project"
    link_project.symlink_to(real_project, target_is_directory=True)
    via_link = link_project / f"{session}.jsonl"

    # Give "first" the canonical path as its transcript_location, with a
    # different session id so the session-id early-return does not fire —
    # the _same_location check is what must allow the re-receipt.
    canonical = str(transcript.resolve())
    first = daemon.store.get_participant("first")
    first.transcript_location = canonical
    first.session_id = "old-session"
    daemon.store.upsert_participant(first)
    daemon.store.record_transcript_receipt(
        "first", session_id="old-session", transcript_location=canonical
    )

    # Monkeypatch the fake observer to return the symlinked path unresolved,
    # so the candidate's location string differs from the stored canonical
    # form. `_same_location` resolves both and reconciles; raw `==` does not.
    def returning_via_link(*, payload, cwd, expected_session_id):
        return TranscriptCandidate(location=str(via_link), session_id=session)

    daemon.observer.harnesses["fake"].observer.validate_transcript_receipt = returning_via_link

    # The re-receipt should be allowed even though "second" is a live
    # same-cwd sibling, because _same_location reconciles the two paths.
    # If the raw `==` were used instead, this call would raise
    # "shares its cwd".
    result = await fake_client.call(
        "transcript.receipt",
        id="first",
        token="one",
        payload={"session_id": session, "path": str(via_link)},
    )
    assert result["ok"] is True
