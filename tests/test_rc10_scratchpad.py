"""Focused RC10 coverage for the shared TTL-aware scratchpad service."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy import BLOB, cast, func, select

from theater.constants.daemon import SCRATCHPAD_NAMESPACE_QUOTA_BYTES
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.scratchpad_handlers import (
    scratchpad_delete as public_delete,
)
from theater.daemon.frontend.scratchpad_handlers import (
    scratchpad_get as public_get,
)
from theater.daemon.frontend.scratchpad_handlers import (
    scratchpad_namespaces as public_namespaces,
)
from theater.daemon.frontend.scratchpad_handlers import (
    scratchpad_write as public_write,
)
from theater.daemon.operations import OperationService
from theater.daemon.persistence.repositories.scratchpad import ScratchpadRepository
from theater.daemon.rpc.scratchpad import _scratchpad_get, _scratchpad_write
from theater.daemon.schema import global_scratchpad
from theater.daemon.scratchpad import ScratchpadService
from theater.frontend.capabilities import ConnectionChannel, ConnectionRole
from theater.models import BadRequest


@dataclass
class Clock:
    value: float

    def __call__(self) -> float:
        return self.value


def _service(store, clock: Clock, *, ttl_days: float = 7.0) -> ScratchpadService:
    return ScratchpadService(store._scratchpad, store.write_unit, ttl_days=ttl_days, clock=clock)


def _daemon(store, clock: Clock, *, ttl_days: float = 7.0) -> SimpleNamespace:
    return SimpleNamespace(
        store=store,
        scratchpad_service=_service(store, clock, ttl_days=ttl_days),
        operation_service=OperationService(store, clock=clock),
    )


def _context(client_id: str = "operator-a") -> ConnectionContext:
    return ConnectionContext(
        client_id=client_id,
        role=ConnectionRole.OPERATOR,
        channel=ConnectionChannel.RPC,
        api_major=1,
        api_minor=0,
        capabilities=frozenset(),
    )


async def test_private_and_public_routes_share_global_entries_without_a_caller(store):
    clock = Clock(1_000.0)
    daemon = _daemon(store, clock)

    private = await _scratchpad_write(
        daemon, {"namespace": "shared", "key": "private", "value": "outside-git"}
    )
    public = await public_get(daemon, _context(), {"namespace": "shared", "limit": 10})
    assert public["items"] == [{"key": private["key"], "value": "outside-git"}]
    assert await public_namespaces(daemon, _context(), {}) == {
        "items": ["shared"],
        "next_cursor": None,
    }

    written = await public_write(
        daemon,
        _context(),
        {"namespace": "shared", "key": "public", "value": "visible privately"},
        idempotency_key="public-write",
    )
    private_page = await _scratchpad_get(daemon, {"namespace": "shared"})
    assert private_page["entries"] == {
        "private": "outside-git",
        written["key"]: "visible privately",
    }
    deleted = await public_delete(
        daemon,
        _context(),
        {"namespace": "shared", "keys": ["private"]},
        idempotency_key="public-delete",
    )
    assert (
        await public_delete(
            daemon,
            _context(),
            {"namespace": "shared", "keys": ["private"]},
            idempotency_key="public-delete",
        )
        == deleted
    )
    assert (await _scratchpad_get(daemon, {"namespace": "shared"}))["entries"] == {
        written["key"]: "visible privately"
    }
    rows = store.conn.execute(select(global_scratchpad).order_by(global_scratchpad.c.key)).all()
    assert [row._mapping["actor_client_id"] for row in rows] == ["operator-a"]


def test_generated_keys_and_key_ordered_paging(store):
    service = _service(store, Clock(10.0))
    generated = service.write(namespace="generated", value="value")
    assert generated
    for key in ("zeta", "alpha", "mid"):
        service.write(namespace="ordered", key=key, value=key)

    first = service.get(namespace="ordered", limit=2)
    assert first.keys == ("alpha", "mid")
    assert first.truncated is True
    assert first.after_key == "mid"
    assert service.get(namespace="ordered", after_key=first.after_key, limit=2).keys == ("zeta",)
    namespaces = service.namespaces(limit=1)
    assert namespaces.namespaces == ("generated",)
    assert namespaces.next_cursor == "generated"
    assert service.namespaces(after_namespace=namespaces.next_cursor, limit=1).namespaces == (
        "ordered",
    )


def test_reads_do_not_extend_expiry(store):
    clock = Clock(100.0)
    service = _service(store, clock, ttl_days=2.0)
    service.write(namespace="ttl", key="note", value="kept")
    row = store.conn.execute(select(global_scratchpad)).one()._mapping
    assert row["updated_at"] == 100.0
    assert row["expires_at"] == 100.0 + 2 * 86_400

    clock.value += 86_400
    assert service.get(namespace="ttl").entries == {"note": "kept"}
    unchanged = store.conn.execute(select(global_scratchpad)).one()._mapping
    assert unchanged["expires_at"] == row["expires_at"]

    clock.value = row["expires_at"]
    assert service.get(namespace="ttl").entries == {}


async def test_idempotent_public_replay_preserves_original_expiry(store):
    clock = Clock(100.0)
    daemon = _daemon(store, clock, ttl_days=1.0)
    params = {"namespace": "ttl", "key": "note", "value": "original"}

    first = await public_write(daemon, _context(), params, idempotency_key="write-once")
    row = store.conn.execute(select(global_scratchpad)).one()._mapping
    original_expiry = row["expires_at"]
    clock.value += 60.0
    replay = await public_write(daemon, _context(), params, idempotency_key="write-once")

    assert replay == first
    persisted = store.conn.execute(select(global_scratchpad)).one()._mapping
    assert persisted["updated_at"] == 100.0
    assert persisted["expires_at"] == original_expiry


def test_expired_rows_leave_namespaces_and_bounded_gc(store):
    clock = Clock(100.0)
    service = _service(store, clock, ttl_days=1 / 86_400)
    service.write(namespace="expired-a", key="a", value="a")
    service.write(namespace="expired-b", key="b", value="b")
    clock.value = 102.0
    service.write(namespace="live", key="c", value="c")

    assert service.namespaces().namespaces == ("live",)
    assert service.delete_expired(limit=1) == 1
    assert service.delete_expired(limit=1) == 1
    assert service.delete_expired(limit=1) == 0
    assert service.namespaces().namespaces == ("live",)


def test_expired_rows_do_not_consume_live_quota_or_entry_count(store, monkeypatch):
    from theater.daemon.persistence.repositories import scratchpad as repository_module

    monkeypatch.setattr(repository_module, "SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE", 1)
    monkeypatch.setattr(repository_module, "SCRATCHPAD_NAMESPACE_QUOTA_BYTES", 8)
    clock = Clock(100.0)
    service = _service(store, clock, ttl_days=1 / 86_400)
    service.write(namespace="reused", key="old", value="12345")
    clock.value = 102.0
    service.write(namespace="reused", key="new", value="12345")

    assert service.get(namespace="reused").entries == {"new": "12345"}


def test_concurrent_quota_boundary_writes_do_not_exceed_global_limit(store):
    repository = ScratchpadRepository(store._db)
    for index in range(3):
        repository.write(
            namespace="contended",
            key=f"base-{index}",
            value="x" * (256 * 1024),
            updated_at=1.0,
            expires_at=10_000.0,
        )
    value = "x" * 140_000

    def attempt(key: str) -> str:
        try:
            with store.engine.begin() as connection:
                repository.write(
                    namespace="contended",
                    key=key,
                    value=value,
                    updated_at=1.0,
                    expires_at=10_000.0,
                    connection=connection,
                )
        except BadRequest:
            return "rejected"
        return "written"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ("a", "b")))

    held = store.conn.execute(
        select(
            func.sum(
                func.length(cast(global_scratchpad.c.key, BLOB))
                + func.length(cast(global_scratchpad.c.value, BLOB))
            )
        ).where(global_scratchpad.c.namespace == "contended")
    ).scalar_one()
    assert sorted(outcomes) == ["rejected", "written"]
    assert held <= SCRATCHPAD_NAMESPACE_QUOTA_BYTES


def test_concurrent_first_writes_are_serialized_before_quota_checks(store, monkeypatch):
    from theater.daemon.persistence.repositories import scratchpad as repository_module

    monkeypatch.setattr(repository_module, "SCRATCHPAD_NAMESPACE_QUOTA_BYTES", 8)
    repository = ScratchpadRepository(store._db)

    def attempt(key: str) -> str:
        try:
            with store.engine.begin() as connection:
                repository.write(
                    namespace="first-writers",
                    key=key,
                    value="12345",
                    updated_at=1.0,
                    expires_at=10_000.0,
                    connection=connection,
                )
        except BadRequest:
            return "rejected"
        return "written"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(attempt, ("a", "b"))) == ["rejected", "written"]


@pytest.mark.parametrize("ttl_days", [False, True, 0, -1, float("inf"), float("nan")])
def test_ttl_days_must_be_positive_and_finite(store, ttl_days):
    with pytest.raises((TypeError, ValueError), match="positive finite"):
        _service(store, Clock(1.0), ttl_days=ttl_days)
