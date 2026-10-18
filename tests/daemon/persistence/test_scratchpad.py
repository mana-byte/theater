"""Scratchpad storage: scoping, quotas, read budgets and TTL-aware expiry."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy import BLOB, cast, func, select

from theater.constants.daemon import (
    SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE,
    SCRATCHPAD_MAX_VALUE_BYTES,
    SCRATCHPAD_NAMESPACE_QUOTA_BYTES,
)
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.scratchpad_handlers import scratchpad_delete as public_delete
from theater.daemon.frontend.scratchpad_handlers import scratchpad_get as public_get
from theater.daemon.frontend.scratchpad_handlers import scratchpad_namespaces as public_namespaces
from theater.daemon.frontend.scratchpad_handlers import scratchpad_write as public_write
from theater.daemon.operations import OperationService
from theater.daemon.persistence.repositories.scratchpad import ScratchpadRepository
from theater.daemon.rpc.scratchpad import _scratchpad_get, _scratchpad_write
from theater.daemon.schema import global_scratchpad
from theater.daemon.scratchpad import ScratchpadService
from theater.frontend.capabilities import ConnectionChannel, ConnectionRole
from theater.models import BadRequest


def test_scratchpad_write_and_get(store):
    key = store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="val1",
        updated_by="p1",
    )
    assert isinstance(key, str) and len(key) > 0
    page = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    assert page.entries == {key: "val1"}
    assert page.keys == (key,)
    assert page.truncated is False
    assert page.after_key is None


def test_scratchpad_get_missing_returns_empty(store):
    page = store.scratchpad_get(tree_root_id="nope", repo_root="/repo", namespace="ns1")
    assert page.entries == {}
    assert page.truncated is False


def test_scratchpad_is_global_across_legacy_tree_scopes(store):
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="from-root1",
        updated_by="p1",
    )
    store.scratchpad_write(
        tree_root_id="root2",
        repo_root="/repo",
        namespace="ns1",
        value="from-root2",
        updated_by="p2",
    )
    r1 = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    r2 = store.scratchpad_get(tree_root_id="root2", repo_root="/repo", namespace="ns1")
    assert set(r1.entries.values()) == {"from-root1", "from-root2"}
    assert r2.entries == r1.entries


def test_scratchpad_isolation_by_namespace(store):
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="from-ns1",
        updated_by="p1",
    )
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns2",
        value="from-ns2",
        updated_by="p1",
    )
    assert list(
        store.scratchpad_get(
            tree_root_id="root1", repo_root="/repo", namespace="ns1"
        ).entries.values()
    ) == ["from-ns1"]
    assert list(
        store.scratchpad_get(
            tree_root_id="root1", repo_root="/repo", namespace="ns2"
        ).entries.values()
    ) == ["from-ns2"]


def test_scratchpad_is_global_across_legacy_repo_scopes(store):
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo-a",
        namespace="ns1",
        value="from-a",
        updated_by="p1",
    )
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo-b",
        namespace="ns1",
        value="from-b",
        updated_by="p1",
    )
    first = store.scratchpad_get(tree_root_id="root1", repo_root="/repo-a", namespace="ns1")
    second = store.scratchpad_get(tree_root_id="root1", repo_root="/repo-b", namespace="ns1")
    assert set(first.entries.values()) == {"from-a", "from-b"}
    assert second.entries == first.entries


def test_scratchpad_read_is_key_ordered_and_cursored(store):
    """Pages are deterministic: ordered by key, continued via after_key."""
    for name in ("zeta", "alpha", "mid"):
        store.scratchpad_write(
            tree_root_id="root1",
            repo_root="/repo",
            namespace="ns1",
            value=f"value-{name}",
            updated_by="p1",
            key=name,
        )
    first = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    assert first.keys == ("alpha", "mid", "zeta")
    assert first.truncated is False
    tail = store.scratchpad_get(
        tree_root_id="root1", repo_root="/repo", namespace="ns1", after_key="mid"
    )
    assert tail.entries == {"zeta": "value-zeta"}


def test_scratchpad_read_budget_pages_by_key(store, monkeypatch):
    """The encoded wire budget truncates a page and names the cursor."""
    from theater.daemon.persistence.repositories import scratchpad as repo_module

    monkeypatch.setattr(repo_module, "SCRATCHPAD_READ_BUDGET_BYTES", 540)
    for name in ("a", "b", "c"):
        store.scratchpad_write(
            tree_root_id="root1",
            repo_root="/repo",
            namespace="ns1",
            value="value-" + name,
            updated_by="p1",
            key=name,
        )
    page = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    # The response wrapper plus one entry's wire cost fits 540; the next crosses it.
    assert page.keys == ("a",)
    assert page.truncated is True
    assert page.after_key == "a"
    rest = store.scratchpad_get(
        tree_root_id="root1", repo_root="/repo", namespace="ns1", after_key=page.after_key
    )
    assert rest.keys == ("b",)
    assert rest.truncated is True


def test_scratchpad_write_refuses_oversized_values(store):
    """One value is bounded; the caller stores a pointer, not the payload."""
    with pytest.raises(BadRequest) as exc:
        store.scratchpad_write(
            tree_root_id="root1",
            repo_root="/repo",
            namespace="ns1",
            value="x" * (SCRATCHPAD_MAX_VALUE_BYTES + 1),
            updated_by="p1",
        )
    assert "bounded" in str(exc.value)


def test_scratchpad_namespace_quota_refuses_writes(store):
    """The aggregate raw UTF-8 bytes of one namespace are bounded."""
    big = "x" * (220 * 1024)
    for index in range(4):
        store.scratchpad_write(
            tree_root_id="root1",
            repo_root="/repo",
            namespace="ns1",
            value=big,
            updated_by="p1",
            key=f"entry-{index}",
        )
    with pytest.raises(BadRequest) as exc:
        store.scratchpad_write(
            tree_root_id="root1",
            repo_root="/repo",
            namespace="ns1",
            value=big,
            updated_by="p1",
            key="entry-over",
        )
    assert "quota" in str(exc.value)


def test_scratchpad_entry_count_bound(store):
    """A namespace holds a bounded number of entries; updates still pass."""
    for index in range(SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE):
        store.scratchpad_write(
            tree_root_id="root1",
            repo_root="/repo",
            namespace="ns1",
            value=str(index),
            updated_by="p1",
            key=f"entry-{index}",
        )
    with pytest.raises(BadRequest) as exc:
        store.scratchpad_write(
            tree_root_id="root1",
            repo_root="/repo",
            namespace="ns1",
            value="one too many",
            updated_by="p1",
            key="entry-extra",
        )
    assert "entries" in str(exc.value)
    # An update to an existing entry does not grow the key space.
    same = store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="still fine",
        updated_by="p1",
        key="entry-0",
    )
    assert same == "entry-0"


def test_scratchpad_delete_is_idempotent(store):
    """Deleting a missing key reports nothing; deleting twice is safe."""
    key = store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="val",
        updated_by="p1",
    )
    assert store.scratchpad_delete(
        tree_root_id="root1", repo_root="/repo", namespace="ns1", keys=[key]
    ) == [key]
    assert (
        store.scratchpad_delete(
            tree_root_id="root1", repo_root="/repo", namespace="ns1", keys=[key]
        )
        == []
    )
    page = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    assert page.entries == {}


def test_scratchpad_read_budget_counts_escaped_wire_bytes(store, monkeypatch):
    """Non-ASCII escapes to six wire bytes each, not two raw UTF-8 bytes."""
    from theater.daemon.persistence.repositories import scratchpad as repo_module

    monkeypatch.setattr(repo_module, "SCRATCHPAD_READ_BUDGET_BYTES", 720)
    # "é" is 2 raw UTF-8 bytes but 6 escaped wire bytes.
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="é" * 30,
        updated_by="p1",
        key="wide",
    )
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="x" * 30,
        updated_by="p1",
        key="thin",
    )
    page = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    # Raw-byte accounting would fit both entries; the wire form stops after "thin".
    assert page.keys == ("thin",)
    assert page.truncated is True


def test_scratchpad_read_marks_an_oversized_first_entry(store, monkeypatch):
    """A legacy entry too big for any page is named, and deletable."""
    from theater.daemon.persistence.repositories import scratchpad as repo_module

    # 518 bytes of namespace echo plus the wrapper, 21 for the entry: the
    # entry busts 530, its 3-byte key still fits beside the namespace.
    monkeypatch.setattr(repo_module, "SCRATCHPAD_READ_BUDGET_BYTES", 530)
    store.scratchpad_write(
        tree_root_id="root1",
        repo_root="/repo",
        namespace="ns1",
        value="value-a",
        updated_by="p1",
        key="a",
    )
    page = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    assert page.oversized_key == "a"
    assert page.entries == {}
    assert store.scratchpad_delete(
        tree_root_id="root1", repo_root="/repo", namespace="ns1", keys=["a"]
    ) == ["a"]
    rest = store.scratchpad_get(tree_root_id="root1", repo_root="/repo", namespace="ns1")
    assert rest.entries == {}
    assert rest.oversized_key is None


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


async def test_a_full_namespace_names_its_oldest_keys_and_lists_without_values(store, monkeypatch):
    from theater.daemon.persistence.repositories import scratchpad as repository_module

    monkeypatch.setattr(repository_module, "SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE", 7)
    clock = Clock(100.0)
    daemon = _daemon(store, clock)
    for i in range(7):
        clock.value = 100.0 + i
        await _scratchpad_write(daemon, {"namespace": "full", "key": f"k{6 - i}", "value": "v"})

    with pytest.raises(BadRequest) as refused:
        await _scratchpad_write(daemon, {"namespace": "full", "key": "new", "value": "v"})
    assert "holds 7 entries and 21 raw UTF-8 bytes" in str(refused.value)
    assert "'k6' (3 B), 'k5' (3 B), 'k4' (3 B), 'k3' (3 B), 'k2' (3 B)" in str(refused.value)
    assert 'scratchpad_delete(keys=["k6", "k5", "k4", "k3", "k2"])' in str(refused.value)

    listed = await _scratchpad_get(daemon, {"namespace": "full", "keys_only": True})
    assert listed["keys"] == [f"k{i}" for i in range(7)]
    assert listed["entries"][0] == {
        "key": "k0",
        "bytes": 3,
        "updated_at": 106.0,
        "expires_at": 106.0 + 7 * 86_400,
        "actor": None,
    }
