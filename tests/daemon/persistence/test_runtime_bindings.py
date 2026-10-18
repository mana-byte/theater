"""Participant runtime bindings: launch intent, identity persistence, lifecycle and bounds."""

from __future__ import annotations

import json

import pytest

from tests.support.runtime_storage import make_binding
from theater.constants.harness import HARNESS_RUNTIME_POLICY_MAX_CHARS
from theater.daemon.persistence.repositories.runtime_bindings import (
    encode_launch_policy,
)
from theater.daemon.spawning.runtime_identity import bind_runtime_identity
from theater.daemon.store import Store
from theater.harness.contracts.runtime import RuntimeBinding, RuntimeLifecyclePhase, RuntimeWiring
from theater.models import Participant, TheaterError, Tier


def test_launch_intent_persists_before_backend_start(store: Store) -> None:
    binding = make_binding()
    with store.runtime_transaction() as conn:
        store.upsert_runtime_binding(binding, connection=conn)
        # The participant reservation would share this transaction.
    persisted = store.get_runtime_binding("p1")
    assert persisted is not None
    assert persisted.lifecycle is RuntimeLifecyclePhase.INTENDED
    assert persisted.native_session_id is None
    assert persisted.backend_pid is None
    assert json.loads(persisted.launch_policy or "{}") == {"approval": "on-request", "model": "m1"}


def test_launch_intent_rolls_back_when_the_transaction_fails(store: Store) -> None:
    with (
        pytest.raises(RuntimeError, match="spawn reservation failed"),
        store.runtime_transaction() as conn,
    ):
        store.upsert_runtime_binding(make_binding(), connection=conn)
        raise RuntimeError("spawn reservation failed")
    # The rolled-back intent is invisible outside the transaction.
    assert store.get_runtime_binding("p1") is None


def test_identity_persists_before_initial_dispatch(store: Store) -> None:
    store.upsert_runtime_binding(make_binding())
    assert store.mark_runtime_backend_started(
        "p1", backend_generation=1, pid=4242, started_at=110.0
    )
    persisted = store.get_runtime_binding("p1")
    assert persisted.lifecycle is RuntimeLifecyclePhase.STARTED
    assert persisted.backend_pid == 4242

    with store.runtime_transaction() as conn:
        assert store.bind_runtime_identity(
            "p1",
            backend_generation=1,
            native_session_id="thread-1",
            protocol="websocket-jsonrpc",
            protocol_version="0.154.0",
            native_version="0.154.0",
            compatibility_policy="codex-0.154-verified",
            updated_at=120.0,
            connection=conn,
        )
    persisted = store.get_runtime_binding("p1")
    assert persisted.lifecycle is RuntimeLifecyclePhase.BOUND
    assert persisted.native_session_id == "thread-1"


def test_runtime_and_participant_identity_rollback_together(store: Store, monkeypatch) -> None:
    participant = Participant(id="p1", harness="codex", tier=Tier.SPAWNED, cwd="/tmp")
    store.upsert_participant(participant)
    store.upsert_runtime_binding(make_binding())

    def fail_participant_write(*_args, **_kwargs):
        raise RuntimeError("injected participant identity failure")

    monkeypatch.setattr(store._participants, "upsert", fail_participant_write)
    with pytest.raises(RuntimeError, match="injected participant identity failure"):
        store.bind_runtime_and_participant_identity(
            "p1",
            backend_generation=1,
            native_session_id="thread-1",
            session_correlation="exact",
            updated_at=120.0,
        )

    assert store.get_runtime_binding("p1").native_session_id is None
    persisted_participant = store.get_participant("p1")
    assert persisted_participant is not None
    assert persisted_participant.session_id is None
    assert persisted_participant.session_correlation is None


@pytest.mark.parametrize(
    ("participant_id", "generation"),
    [("foreign", 1), ("p1", 99)],
)
def test_returned_runtime_identity_is_validated_before_persistence(
    store: Store,
    participant_id: str,
    generation: int,
) -> None:
    participant = Participant(id="p1", harness="codex", tier=Tier.SPAWNED, cwd="/tmp")
    store.upsert_participant(participant)
    store.upsert_runtime_binding(make_binding())
    returned = RuntimeBinding(
        participant_id=participant_id,
        backend_generation=generation,
        wiring=RuntimeWiring.NATIVE,
        lifecycle=RuntimeLifecyclePhase.BOUND,
        endpoint="unix:///tmp/private.sock",
        native_session_id="foreign-session",
    )

    with pytest.raises(TheaterError):
        bind_runtime_identity(store, "p1", returned, 1)

    assert store.get_runtime_binding("p1").native_session_id is None
    persisted_participant = store.get_participant("p1")
    assert persisted_participant is not None and persisted_participant.session_id is None


def test_recoverable_bindings_exclude_terminal_phases(store: Store) -> None:
    store.upsert_runtime_binding(make_binding("p1"))
    store.upsert_runtime_binding(make_binding("p2", lifecycle=RuntimeLifecyclePhase.DETACHED))
    store.upsert_runtime_binding(make_binding("p3", lifecycle=RuntimeLifecyclePhase.STOPPED))
    store.upsert_runtime_binding(
        make_binding("p4", wiring=RuntimeWiring.LEGACY, lifecycle=RuntimeLifecyclePhase.FAILED)
    )
    recoverable = {binding.participant_id for binding in store.runtime_bindings_for_recovery()}
    assert recoverable == {"p1", "p2"}


def test_binding_lookup_by_exact_native_session(store: Store) -> None:
    store.upsert_runtime_binding(make_binding())
    store.bind_runtime_identity(
        "p1",
        backend_generation=1,
        native_session_id="thread-1",
        updated_at=120.0,
    )
    assert store.runtime_binding_by_native_session("thread-1") is not None
    assert store.runtime_binding_by_native_session("thread-2") is None


def test_binding_lifecycle_transitions_and_delete(store: Store) -> None:
    store.upsert_runtime_binding(make_binding())
    assert store.set_runtime_lifecycle(
        "p1", RuntimeLifecyclePhase.ACTIVE, backend_generation=1, updated_at=130.0
    )
    assert store.get_runtime_binding("p1").lifecycle is RuntimeLifecyclePhase.ACTIVE
    store.delete_runtime_binding("p1")
    assert store.get_runtime_binding("p1") is None


def test_stale_generation_cannot_mutate_the_current_generation(store: Store) -> None:
    """A delayed generation-1 callback must never touch generation 2's row."""
    store.upsert_runtime_binding(make_binding())
    store.upsert_runtime_binding(
        make_binding(
            backend_generation=2,
            lifecycle=RuntimeLifecyclePhase.BOUND,
            endpoint="unix:///tmp/private-2.sock",
        )
    )
    store.mark_runtime_backend_started("p1", backend_generation=2, pid=9999, started_at=200.0)
    store.bind_runtime_identity(
        "p1", backend_generation=2, native_session_id="thread-2", updated_at=210.0
    )

    # Every stale generation-1 mutation fails explicitly and changes nothing.
    assert (
        store.mark_runtime_backend_started("p1", backend_generation=1, pid=1111, started_at=220.0)
        is False
    )
    assert (
        store.bind_runtime_identity(
            "p1", backend_generation=1, native_session_id="thread-stale", updated_at=230.0
        )
        is False
    )
    assert (
        store.set_runtime_lifecycle(
            "p1", RuntimeLifecyclePhase.ATTACHED, backend_generation=1, updated_at=240.0
        )
        is False
    )

    persisted = store.get_runtime_binding("p1")
    assert persisted.backend_generation == 2
    assert persisted.backend_pid == 9999
    assert persisted.native_session_id == "thread-2"
    assert persisted.lifecycle is RuntimeLifecyclePhase.BOUND


def test_encode_launch_policy_bounds_encoded_utf8_bytes() -> None:
    # A NaN float is not a finite JSON number, so encoding is rejected.
    with pytest.raises(ValueError, match="finite"):
        encode_launch_policy({"loss": float("nan")})
    # The encoded form must fit the declared byte bound, not just be valid JSON.
    oversized = {f"policy-{index:05d}": "x" * 64 for index in range(2000)}
    assert len(json.dumps(oversized).encode("utf-8")) > 65_536
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        encode_launch_policy(oversized)
    assert encode_launch_policy({"approval": "on-request"}) == '{"approval":"on-request"}'


def test_binding_rejects_malformed_or_oversized_launch_policy(store: Store) -> None:
    with pytest.raises(ValueError, match="JSON object"):
        store.upsert_runtime_binding(make_binding(launch_policy="not json"))
    with pytest.raises(ValueError, match="JSON object"):
        store.upsert_runtime_binding(
            make_binding(launch_policy=json.dumps(["not", "an", "object"]))
        )
    # NaN decodes as a float in Python's JSON, but is not a finite JSON number.
    with pytest.raises(ValueError, match="finite"):
        store.upsert_runtime_binding(make_binding(launch_policy='{"loss": NaN}'))
    with pytest.raises(ValueError, match="compatibility_policy"):
        store.upsert_runtime_binding(
            make_binding(compatibility_policy="x" * (HARNESS_RUNTIME_POLICY_MAX_CHARS + 1))
        )
    assert store.get_runtime_binding("p1") is None
