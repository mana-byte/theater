"""Scratchpad RPC read budgets, oversized legacy entries and bounded deletes."""

from __future__ import annotations

import hashlib

import pytest

from tests.support.git_repos import init_repo
from theater.protocol import RemoteError


async def test_scratchpad_get_rejects_empty_and_overlong_requested_keys(client, tmp_path):
    """Each requested key is non-empty and within the name bound."""
    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.get",
            caller_id=caller["id"],
            namespace="notes",
            keys=[""],
        )
    assert exc.value.code == "bad_request"
    assert "non-empty" in str(exc.value)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.get",
            caller_id=caller["id"],
            namespace="notes",
            keys=["k" * 129],
        )
    assert exc.value.code == "bad_request"
    assert "bounded to" in str(exc.value)


async def test_scratchpad_get_accepts_an_overlong_after_key_cursor(client, tmp_path):
    """A page cursor may name any stored key, even a pre-bound legacy one."""
    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
        after_key="k" * 129,
    )
    assert got == {
        "namespace": "notes",
        "entries": {},
        "keys": [],
        "truncated": False,
        "after_key": None,
    }


async def test_scratchpad_get_names_an_oversized_legacy_entry(client, tmp_path, monkeypatch):
    """A pre-bound oversized entry is refused by a page naming it to delete."""
    from theater.daemon.persistence.repositories import scratchpad as repo_module

    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))
    await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="value-a",
        key="a",
    )
    # 519 bytes of namespace echo plus the wrapper, 21 for the entry: the
    # entry busts 530, its 3-byte key still fits beside the namespace.
    monkeypatch.setattr(repo_module, "SCRATCHPAD_READ_BUDGET_BYTES", 530)
    got = await client.call("scratchpad.get", caller_id=caller["id"], namespace="notes")
    assert got["entries"] == {}
    assert got["truncated"] is True
    assert got["oversized_key"] == "a"
    assert got["oversized_digest"] == hashlib.sha256(b"a").hexdigest()
    assert got["oversized_bytes"] > 0
    # deleting the named entry unblocks the namespace
    deleted = await client.call(
        "scratchpad.delete",
        caller_id=caller["id"],
        namespace="notes",
        keys=["a"],
    )
    assert deleted == {"namespace": "notes", "deleted": ["a"]}
    got = await client.call("scratchpad.get", caller_id=caller["id"], namespace="notes")
    assert got == {
        "namespace": "notes",
        "entries": {},
        "keys": [],
        "truncated": False,
        "after_key": None,
    }


async def test_scratchpad_get_refuses_a_namespace_beyond_the_read_budget(
    client, tmp_path, monkeypatch
):
    """The echoed namespace is part of the response, so it faces the budget."""
    from theater.daemon import scratchpad_operations as operations_module

    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(operations_module, "SCRATCHPAD_READ_BUDGET_BYTES", 512)
        with pytest.raises(RemoteError) as exc:
            await client.call(
                "scratchpad.get",
                caller_id=caller["id"],
                namespace="n" * 600,
            )
        assert exc.value.code == "bad_request"
        assert "602 wire bytes" in str(exc.value)
        assert "scratchpad.delete" in str(exc.value)
        # wire bytes alone fit the budget; the fixed overhead does not
        with pytest.raises(RemoteError) as exc:
            await client.call(
                "scratchpad.get",
                caller_id=caller["id"],
                namespace="n" * 510,
            )
    assert exc.value.code == "bad_request"


async def test_scratchpad_get_reports_an_unnameable_oversized_key_by_size(
    client, tmp_path, monkeypatch
):
    """A legacy key too large to echo is refused by size, not by name."""
    from theater.daemon import scratchpad_operations as operations_module
    from theater.daemon.persistence.repositories import scratchpad as repo_module

    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))
    legacy = "k" * 1_500_000
    with pytest.MonkeyPatch.context() as mp:
        # Write while the bounds are raised, so the entry predates them.
        mp.setattr(operations_module, "SCRATCHPAD_MAX_NAME_LENGTH", 1_600_000)
        mp.setattr(repo_module, "SCRATCHPAD_NAMESPACE_QUOTA_BYTES", 8 * 1024 * 1024)
        await client.call(
            "scratchpad.write",
            caller_id=caller["id"],
            namespace="notes",
            value="v",
            key=legacy,
        )
    # a reader who never wrote the entry sees a digest, not a name
    got = await client.call("scratchpad.get", caller_id=caller["id"], namespace="notes")
    assert got["entries"] == {}
    assert got["truncated"] is True
    assert got["oversized_key"] is None
    digest = got["oversized_digest"]
    assert digest == hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    assert got["oversized_bytes"] > 0
    # the digest deletes what the name cannot; the response confirms by digest
    deleted = await client.call(
        "scratchpad.delete",
        caller_id=caller["id"],
        namespace="notes",
        digests=[digest],
    )
    assert deleted == {
        "namespace": "notes",
        "deleted": [],
        "deleted_oversized": [digest],
    }


async def test_scratchpad_delete_by_name_confirms_a_mega_key_by_digest(
    client, tmp_path, monkeypatch
):
    """Deleting a too-large key by name confirms it by digest, not echo."""
    from theater.daemon import scratchpad_operations as operations_module
    from theater.daemon.persistence.repositories import scratchpad as repo_module

    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))
    legacy = "k" * 1_500_000
    digest = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    with pytest.MonkeyPatch.context() as mp:
        # Write while the bounds are raised, so the entry predates them.
        mp.setattr(operations_module, "SCRATCHPAD_MAX_NAME_LENGTH", 1_600_000)
        mp.setattr(repo_module, "SCRATCHPAD_NAMESPACE_QUOTA_BYTES", 8 * 1024 * 1024)
        await client.call(
            "scratchpad.write",
            caller_id=caller["id"],
            namespace="notes",
            value="v",
            key=legacy,
        )
    deleted = await client.call(
        "scratchpad.delete",
        caller_id=caller["id"],
        namespace="notes",
        keys=[legacy],
    )
    assert deleted == {
        "namespace": "notes",
        "deleted": [],
        "deleted_oversized": [digest],
    }
    rest = await client.call("scratchpad.get", caller_id=caller["id"], namespace="notes")
    assert rest["entries"] == {}


async def test_scratchpad_delete_validates_the_digests_parameter(client, tmp_path):
    """Digest deletions are 32-char lowercase hex, count-bounded, and a
    request needs at least one key or digest."""
    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
            digests=["a" * 63],
        )
    assert exc.value.code == "bad_request"
    assert "64-character" in str(exc.value)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
            digests=["g" * 64],
        )
    assert exc.value.code == "bad_request"

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
            digests=[123],
        )
    assert exc.value.code == "bad_request"
    assert "list of strings" in str(exc.value)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
            digests=["a" * 32 for _ in range(129)],
        )
    assert exc.value.code == "bad_request"
    assert "bounded to" in str(exc.value)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
        )
    assert exc.value.code == "bad_request"
    assert "at least one" in str(exc.value)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
            keys=["a"],
            clear=True,
        )
    assert exc.value.code == "bad_request"
    assert "empties the namespace" in str(exc.value)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
            clear="yes",
        )
    assert exc.value.code == "bad_request"
    assert "must be a boolean" in str(exc.value)

    # null counts as absent, so the request still needs a selector
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
            clear=None,
        )
    assert exc.value.code == "bad_request"
    assert "at least one" in str(exc.value)


async def test_scratchpad_clear_empties_an_unreadable_namespace(client, tmp_path):
    """The only remedy for a namespace too large to echo: clear it."""
    from theater.daemon import scratchpad_operations as operations_module

    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))
    await client.call(
        "scratchpad.write", caller_id=caller["id"], namespace="notes", value="1", key="a"
    )
    await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="2",
        key="second",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(operations_module, "SCRATCHPAD_READ_BUDGET_BYTES", 512)
        with pytest.raises(RemoteError):
            await client.call("scratchpad.get", caller_id=caller["id"], namespace="notes")
        cleared = await client.call(
            "scratchpad.delete", caller_id=caller["id"], namespace="notes", clear=True
        )
    # the namespace echo cannot fit 512 bytes, so it comes back null
    assert cleared == {"namespace": None, "deleted_count": 2}
    rest = await client.call("scratchpad.get", caller_id=caller["id"], namespace="notes")
    assert rest["entries"] == {}


async def test_scratchpad_delete_refuses_an_ambiguous_digest(client, tmp_path, monkeypatch):
    """A digest matching several entries would guess; it refuses."""
    from theater.daemon.persistence.repositories import scratchpad as repo_module

    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))
    await client.call(
        "scratchpad.write", caller_id=caller["id"], namespace="notes", value="1", key="a"
    )
    await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="2",
        key="second",
    )

    class _StubDigest:
        def __init__(self, data):
            pass

        def hexdigest(self):
            return "f" * 64

    monkeypatch.setattr(repo_module.hashlib, "sha256", _StubDigest)
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
            digests=["f" * 64],
        )
    assert exc.value.code == "bad_request"
    assert "matches 2 entries" in str(exc.value)


async def test_scratchpad_delete_response_stays_within_the_read_budget(client, tmp_path):
    """Names echo only while they fit; the rest are confirmed by digest."""
    from theater.daemon import scratchpad_operations as operations_module

    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))
    await client.call(
        "scratchpad.write", caller_id=caller["id"], namespace="notes", value="1", key="a"
    )
    await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="2",
        key="second",
    )
    with pytest.MonkeyPatch.context() as mp:
        # no name can fit: both come back as digests, the echo as null
        mp.setattr(operations_module, "SCRATCHPAD_READ_BUDGET_BYTES", 0)
        deleted = await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
            keys=["a", "second"],
        )
    assert deleted == {
        "namespace": None,
        "deleted": [],
        "deleted_oversized": [
            hashlib.sha256(b"a").hexdigest(),
            hashlib.sha256(b"second").hexdigest(),
        ],
    }


async def test_scratchpad_delete_removes_named_keys(client, tmp_path):
    """The bounded keys collection deletes what exists, in key order."""
    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))
    await client.call(
        "scratchpad.write", caller_id=caller["id"], namespace="notes", value="a", key="a"
    )
    await client.call(
        "scratchpad.write", caller_id=caller["id"], namespace="notes", value="b", key="b"
    )

    deleted = await client.call(
        "scratchpad.delete",
        caller_id=caller["id"],
        namespace="notes",
        keys=["b", "a", "missing"],
    )
    assert deleted == {"namespace": "notes", "deleted": ["a", "b"]}
    got = await client.call("scratchpad.get", caller_id=caller["id"], namespace="notes")
    assert got["entries"] == {}


async def test_scratchpad_delete_rejects_empty_strings_and_unbounded_batches(client, tmp_path):
    """Requested deletions are non-empty and count-bounded."""
    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
            keys=[""],
        )
    assert exc.value.code == "bad_request"
    assert "non-empty" in str(exc.value)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.delete",
            caller_id=caller["id"],
            namespace="notes",
            keys=[f"k{i}" for i in range(129)],
        )
    assert exc.value.code == "bad_request"
    assert "bounded to" in str(exc.value)


async def test_scratchpad_delete_accepts_a_legacy_overlong_key(client, tmp_path, monkeypatch):
    """Pre-bound keys stay deletable so they can be cleaned up."""
    from theater.daemon import scratchpad_operations as operations_module

    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))
    legacy = "k" * 200
    with pytest.MonkeyPatch.context() as mp:
        # Write while the bound is raised, so the entry predates it.
        mp.setattr(operations_module, "SCRATCHPAD_MAX_NAME_LENGTH", 300)
        await client.call(
            "scratchpad.write",
            caller_id=caller["id"],
            namespace="notes",
            value="legacy",
            key=legacy,
        )
    # keyed reads refuse the overlong name; full reads still list it
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.get",
            caller_id=caller["id"],
            namespace="notes",
            keys=[legacy],
        )
    assert exc.value.code == "bad_request"
    full = await client.call("scratchpad.get", caller_id=caller["id"], namespace="notes")
    assert full["keys"] == [legacy]
    deleted = await client.call(
        "scratchpad.delete",
        caller_id=caller["id"],
        namespace="notes",
        keys=[legacy],
    )
    assert deleted == {"namespace": "notes", "deleted": [legacy]}
    rest = await client.call("scratchpad.get", caller_id=caller["id"], namespace="notes")
    assert rest["keys"] == []


async def test_scratchpad_legacy_overlong_namespace_stays_readable_and_deletable(
    client, tmp_path, monkeypatch
):
    """Pre-bound namespaces grandfather like keys: readable, deletable."""
    from theater.daemon import scratchpad_operations as operations_module

    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))
    legacy_ns = "n" * 200
    with pytest.MonkeyPatch.context() as mp:
        # Write while the bound is raised, so the namespace predates it.
        mp.setattr(operations_module, "SCRATCHPAD_MAX_NAME_LENGTH", 300)
        await client.call(
            "scratchpad.write",
            caller_id=caller["id"],
            namespace=legacy_ns,
            value="legacy",
            key="k",
        )
    # reads and deletes still address the pre-bound namespace
    page = await client.call("scratchpad.get", caller_id=caller["id"], namespace=legacy_ns)
    assert page["entries"] == {"k": "legacy"}
    deleted = await client.call(
        "scratchpad.delete", caller_id=caller["id"], namespace=legacy_ns, keys=["k"]
    )
    assert deleted == {"namespace": legacy_ns, "deleted": ["k"]}
    rest = await client.call("scratchpad.get", caller_id=caller["id"], namespace=legacy_ns)
    assert rest["entries"] == {}
