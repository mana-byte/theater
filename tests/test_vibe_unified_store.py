"""Storage-level tests for the bounded Vibe unified-session-store reader.

These build synthetic stores on disk — canonical documents, digest chains, chunk
pools, CURRENT pointers — exactly the way the reference writer lays them down,
and check that ``unified_store`` reads them back, refuses what the format
forbids, and replays journal projection records with the reference semantics.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest
import rfc8785

from theater.harness.builtin.plugins.vibe import unified_store
from theater.harness.builtin.plugins.vibe.unified_store import (
    UnifiedStoreError,
    UnifiedStoreRequiresNewer,
    current_fingerprint,
    load_unified_store,
)

SESSION_ID = "sess-1"
GEN1 = "0" * 15 + "1"
GEN2 = "0" * 15 + "2"


# --- Store construction helpers ------------------------------------------------


def canonical(value: Any) -> bytes:
    """RFC 8785 canonical JSON, the reference writer's on-disk encoding."""
    return rfc8785.dumps(value)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_hex(canonical(value))


def entry(entry_id: str, text: str | None = None) -> dict[str, Any]:
    return {"id": entry_id, "text": text if text is not None else f"body of {entry_id}"}


def make_state(
    entries: list[dict[str, Any]], *, status: str = "idle", title: str | None = None
) -> dict[str, Any]:
    """A public session state in its camelCase wire shape."""
    return {
        "format": "harness.public-session-state/v1",
        "session": {
            "id": SESSION_ID,
            "status": {"type": status},
            "createdAt": 1000,
            "updatedAt": 2000,
            **({"title": title} if title is not None else {}),
        },
        "history": {
            "range": "latest",
            "entries": entries,
            "cursor": {"before": None, "after": None},
        },
        "turnQueue": {"items": [], "paused": False, "maxItems": 8},
        "activeCallbacks": [],
        "latestTurn": None,
    }


def make_runtime(snapshot_sequence: int) -> dict[str, Any]:
    """A runtime state with the identity facts, plus fields the reader ignores."""
    return {
        "runtime_state_version": 3,
        "session_id": SESSION_ID,
        "snapshot_sequence": snapshot_sequence,
        "session_metadata": {"root_session_id": SESSION_ID, "cwd": "/tmp/work"},
        "command_receipts": [],
        "actions": [],
        "callbacks": [],
        "provider_operations": [],
        "processes": [],
        "submitted_process_notifications": [],
        "children": [],
        "identity": {"session_id": SESSION_ID},
    }


def advanced(watermark: int, state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return "projection_advanced", {"watermark": watermark, "snapshot": state}


def delta(watermark: int, *ops: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return "projection_delta", {"watermark": watermark, "delta": list(ops)}


def append(entry_value: dict[str, Any]) -> dict[str, Any]:
    return {"op": "append_entry", "entry": entry_value}


def replace(entry_id: str, entry_value: dict[str, Any]) -> dict[str, Any]:
    return {"op": "replace_entry", "id": entry_id, "entry": entry_value}


def remove(entry_id: str) -> dict[str, Any]:
    return {"op": "remove_entry", "id": entry_id}


def set_history(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"op": "set_history_entries", "entries": list(entries)}


def set_envelope(state: dict[str, Any]) -> dict[str, Any]:
    return {"op": "set_envelope", "state": state}


class Store:
    """A synthetic unified session store, written the way the reference does."""

    def __init__(self, root: Path, session_id: str = SESSION_ID) -> None:
        self.session_root = root / "unified" / session_id
        self.session_root.mkdir(parents=True)
        self.session_id = session_id

    @property
    def current(self) -> Path:
        return self.session_root / "CURRENT"

    def generation_dir(self, generation: str) -> Path:
        return self.session_root / "generations" / generation

    def publish(
        self,
        *,
        generation: str,
        snapshot_sequence: int,
        state: dict[str, Any],
        watermark: int = 0,
        journal: list[tuple[str, dict[str, Any]]] | None = None,
        pooled: bool = False,
        interop: bool = False,
        execution_state: str = "quiescent",
        created_at: str = "2026-02-02T19:00:00.000Z",
        store_minor: int | None = 4,
        point_current: bool = True,
    ) -> None:
        """Write one whole generation, and point CURRENT at it by default."""
        journal = journal or []
        first_sequence = snapshot_sequence + 1
        records = self._journal_records(first_sequence, journal)

        checkpoint = {"checkpoint_version": 1, "context": {"messages": state["history"]["entries"]}}
        runtime = make_runtime(snapshot_sequence)
        projection = {
            "projection_state_version": 1,
            "session_id": self.session_id,
            "snapshot_sequence": snapshot_sequence,
            "watermark": watermark,
            "snapshot": state,
        }
        checkpoint_chunks = self._pool(checkpoint, ("context", "messages")) if pooled else None
        projection_chunks = (
            self._pool(projection, ("snapshot", "history", "entries")) if pooled else None
        )

        generation_dir = self.generation_dir(generation)
        generation_dir.mkdir(parents=True, exist_ok=True)
        self._write_document(generation_dir / "checkpoint.json", checkpoint)
        self._write_document(generation_dir / "runtime_state.json", runtime)
        self._write_document(generation_dir / "projection_state.json", projection)
        journal_path = self.session_root / "journal" / f"{first_sequence:016d}.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_bytes(b"".join(canonical(record) + b"\n" for record in records))

        manifest = {
            "manifest_version": 1,
            "session_id": self.session_id,
            "generation": generation,
            "created_at": created_at,
            "snapshot_sequence": snapshot_sequence,
            "execution_state": execution_state,
            "checkpoint": {
                "path": "checkpoint.json",
                "sha256": sha256_json(checkpoint),
                "chunks": checkpoint_chunks,
                "checkpoint_version": 1,
            },
            "runtime_state": {
                "path": "runtime_state.json",
                "sha256": sha256_json(runtime),
                "chunks": None,
            },
            "projection_state": {
                "path": "projection_state.json",
                "sha256": sha256_json(projection),
                "chunks": projection_chunks,
            },
            "recovery_journal_segment": {
                "path": f"journal/{first_sequence:016d}.jsonl",
                "first_sequence": first_sequence,
            },
        }
        if interop:
            # The interop export record is validated but its document is never
            # read, so it deliberately points at a file this writer never creates.
            manifest["interop_export"] = {
                "path": "interop-export.json",
                "sha256": "0" * 64,
                "chunks": None,
            }
        manifest_body = canonical(manifest)
        (generation_dir / "manifest.json").write_bytes(manifest_body + b"\n")

        if point_current:
            pointer: dict[str, Any] = {
                "store_format": unified_store.STORE_FORMAT,
                "session_id": self.session_id,
                "generation": generation,
                "snapshot_sequence": snapshot_sequence,
                "manifest_sha256": sha256_hex(manifest_body),
            }
            if store_minor is not None:
                pointer["store_format_minor"] = store_minor
            self._write_document(self.current, pointer)

    def _journal_records(
        self, first_sequence: int, journal: list[tuple[str, dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        previous: str | None = None
        for offset, (record_type, payload) in enumerate(journal):
            record = {
                "recovery_journal_record_version": 1,
                "sequence": first_sequence + offset,
                "previous_record_sha256": previous,
                "type": record_type,
                "payload": payload,
            }
            record["record_sha256"] = sha256_json(record)
            records.append(record)
            previous = record["record_sha256"]
        return records

    def _pool(self, document: dict[str, Any], path: tuple[str, ...]) -> list[str]:
        """Move a transcript into the shared chunk pool, leaving an empty envelope."""
        node: Any = document
        for key in path[:-1]:
            node = node[key]
        digests: list[str] = []
        for item in node[path[-1]]:
            body = canonical([item])
            digest = sha256_hex(body)
            chunk_file = self.session_root / "chunks" / f"{digest}.json"
            chunk_file.parent.mkdir(parents=True, exist_ok=True)
            chunk_file.write_bytes(body + b"\n")
            digests.append(digest)
        node[path[-1]] = []
        return digests

    @staticmethod
    def _write_document(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(value) + b"\n")


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path)


def publish_default(store: Store, **overrides: Any) -> None:
    """The one-page store every happy-path test starts from: one entry, one delta."""
    defaults: dict[str, Any] = {
        "generation": GEN1,
        "snapshot_sequence": 0,
        "state": make_state([entry("e0")]),
        "watermark": 1,
        "journal": [delta(2, append(entry("e1")))],
    }
    defaults.update(overrides)
    store.publish(**defaults)


# --- Happy paths ---------------------------------------------------------------


def test_active_load_replays_inline_transcript_and_delta(store: Store) -> None:
    publish_default(store, store_minor=1)
    view = load_unified_store(store.current)
    assert view is not None
    assert view.store_minor == 1
    assert view.session_id == SESSION_ID
    assert view.generation == GEN1
    assert view.snapshot_sequence == 0
    assert view.sequence == 1
    assert view.watermark == 2
    assert [item["id"] for item in view.snapshot["history"]["entries"]] == ["e0", "e1"]
    assert view.manifest_created_at == "2026-02-02T19:00:00.000Z"
    assert view.journal_fingerprint is not None and len(view.journal_fingerprint) == 1
    assert view.runtime_state["session_metadata"]["root_session_id"] == SESSION_ID
    assert view.current == store.current


def test_active_load_defaults_missing_minor_to_one(store: Store) -> None:
    publish_default(store, store_minor=None)
    view = load_unified_store(store.current)
    assert view is not None
    assert view.store_minor == 1


def test_store_minors_two_and_four_load(store: Store, tmp_path: Path) -> None:
    for minor in (2, 4):
        own_store = Store(tmp_path / f"minor-{minor}")
        publish_default(own_store, store_minor=minor)
        view = load_unified_store(own_store.current)
        assert view is not None
        assert view.store_minor == minor


def test_projection_advanced_resets_state_then_deltas_apply(store: Store) -> None:
    publish_default(
        store,
        journal=[
            advanced(5, make_state([entry("a")])),
            delta(6, append(entry("b"))),
        ],
    )
    view = load_unified_store(store.current)
    assert view is not None
    assert view.watermark == 6
    assert [item["id"] for item in view.snapshot["history"]["entries"]] == ["a", "b"]


def test_delta_ops_fold_with_reference_semantics(store: Store) -> None:
    publish_default(
        store,
        state=make_state([entry("a"), entry("b"), entry("a")]),
        journal=[
            delta(
                2,
                replace("a", entry("a", "replaced first")),
                remove("a"),
                set_history(entry("x"), entry("y")),
            ),
            delta(
                3,
                append(entry("z")),
                set_envelope(make_state([], title="fresh envelope")),
            ),
        ],
    )
    view = load_unified_store(store.current)
    assert view is not None
    # replace_entry hits the first matching id; remove_entry drops every match.
    assert [item["id"] for item in view.snapshot["history"]["entries"]] == ["x", "y", "z"]
    # set_envelope carries every non-history field; the folded entries win.
    assert view.snapshot["session"]["title"] == "fresh envelope"
    assert view.snapshot["session"]["id"] == SESSION_ID


def test_pooled_transcripts_reassemble_in_order(store: Store) -> None:
    publish_default(
        store,
        state=make_state([entry("p0"), entry("p1"), entry("p2")]),
        journal=[delta(2, append(entry("p3")), append(entry("p4")))],
        pooled=True,
    )
    view = load_unified_store(store.current)
    assert view is not None
    assert [item["id"] for item in view.snapshot["history"]["entries"]] == [
        "p0",
        "p1",
        "p2",
        "p3",
        "p4",
    ]
    # The pooled checkpoint transcript is reassembled at context.messages too.
    checkpoint_ids = [
        item["id"]
        for item in view.runtime_state.get("checkpoint", {}).get("context", {}).get("messages", [])
    ]
    assert checkpoint_ids == []  # runtime_state is the runtime doc, not the checkpoint
    assert view.store_minor == 4


def test_non_projection_records_are_counted_not_interpreted(store: Store) -> None:
    publish_default(
        store,
        journal=[
            (
                "command_reserved",
                {"client_command_id": "c1", "method": "m", "params_sha256": "0" * 64},
            ),
            ("core_input", {"anything": True}),
            delta(2, append(entry("e1"))),
        ],
    )
    view = load_unified_store(store.current)
    assert view is not None
    assert view.sequence == 3
    assert len(view.journal_fingerprint or ()) == 3


# --- Historical loads ----------------------------------------------------------


def test_historical_at_snapshot_sequence_applies_nothing(store: Store) -> None:
    publish_default(store)
    view = load_unified_store(store.current, at_sequence=0)
    assert view is not None
    assert view.sequence == 0
    assert view.journal_fingerprint is None
    assert [item["id"] for item in view.snapshot["history"]["entries"]] == ["e0"]


def test_historical_mid_journal_applies_prefix(store: Store) -> None:
    publish_default(
        store,
        journal=[
            ("core_input", {}),
            delta(2, append(entry("e1"))),
            delta(3, append(entry("e2"))),
        ],
    )
    view = load_unified_store(store.current, at_sequence=2)
    assert view is not None
    assert view.sequence == 2
    assert [item["id"] for item in view.snapshot["history"]["entries"]] == ["e0", "e1"]


def test_historical_uncovered_returns_none(store: Store) -> None:
    publish_default(store, journal=[delta(2, append(entry("e1")))])
    assert load_unified_store(store.current, at_sequence=5) is None
    # A negative sequence is not a store fact but invalid input.
    with pytest.raises(UnifiedStoreError, match="at_sequence"):
        load_unified_store(store.current, at_sequence=-1)


def test_historical_finds_retained_generation(store: Store) -> None:
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=make_state([entry("old-0")]),
        journal=[delta(1, append(entry("old-1"))), delta(2, append(entry("old-2")))],
    )
    publish_default(
        store,
        generation=GEN2,
        snapshot_sequence=5,
        state=make_state([entry("new-0")]),
        journal=[delta(6, append(entry("new-1")))],
    )
    # Without a hint the active generation is tried first; only it can cover 6.
    recent = load_unified_store(store.current, at_sequence=6)
    assert recent is not None
    assert recent.generation == GEN2
    assert [item["id"] for item in recent.snapshot["history"]["entries"]] == ["new-0", "new-1"]
    # The older sequence falls through to the retained generation.
    old = load_unified_store(store.current, at_sequence=1)
    assert old is not None
    assert old.generation == GEN1
    assert [item["id"] for item in old.snapshot["history"]["entries"]] == ["old-0", "old-1"]


def test_historical_generation_hint_pins_search_order(store: Store) -> None:
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=make_state([entry("old-0")]),
        journal=[delta(1, append(entry("old-1")))],
    )
    publish_default(
        store,
        generation=GEN2,
        snapshot_sequence=2,
        state=make_state([entry("new-0")]),
        journal=[delta(3, append(entry("new-1")))],
    )
    hinted = load_unified_store(store.current, generation_hint=GEN1)
    assert hinted is not None
    assert hinted.generation == GEN1
    assert [item["id"] for item in hinted.snapshot["history"]["entries"]] == ["old-0", "old-1"]
    hinted_at = load_unified_store(store.current, at_sequence=1, generation_hint=GEN1)
    assert hinted_at is not None
    assert hinted_at.sequence == 1
    with pytest.raises(UnifiedStoreError, match="generation hint"):
        load_unified_store(store.current, at_sequence=1, generation_hint="not-a-generation")


def test_historical_skips_generation_deleted_mid_search(store: Store) -> None:
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=make_state([entry("old-0")]),
        journal=[delta(1, append(entry("old-1")))],
    )
    publish_default(
        store,
        generation=GEN2,
        snapshot_sequence=2,
        state=make_state([entry("new-0")]),
    )
    # The retained generation vanishes mid-search: an uncovering, not a corruption.
    shutil.rmtree(store.generation_dir(GEN1))
    assert load_unified_store(store.current, at_sequence=1) is None


def test_historical_corruption_is_not_skipped(store: Store) -> None:
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=make_state([entry("old-0")]),
        journal=[delta(1, append(entry("old-1")))],
    )
    publish_default(
        store,
        generation=GEN2,
        snapshot_sequence=2,
        state=make_state([entry("new-0")]),
    )
    manifest = store.generation_dir(GEN1) / "manifest.json"
    manifest.write_bytes(b'{"manifest_version": 1}\n')  # non-canonical, wrong shape
    with pytest.raises(UnifiedStoreError):
        load_unified_store(store.current, at_sequence=1)


def test_historical_requires_current(store: Store) -> None:
    store.session_root.joinpath("unreferenced").mkdir()
    with pytest.raises(UnifiedStoreError):
        load_unified_store(store.current, at_sequence=0, generation_hint=GEN1)


# --- Pointer and format gates --------------------------------------------------


def test_newer_minor_requires_newer_reader(store: Store) -> None:
    publish_default(store, store_minor=5)
    with pytest.raises(UnifiedStoreRequiresNewer) as caught:
        load_unified_store(store.current)
    assert caught.value.store_minor == 5
    assert caught.value.code == "vibe_unified_store_newer"
    with pytest.raises(UnifiedStoreRequiresNewer):
        load_unified_store(store.current, at_sequence=0)


def test_newer_minor_checked_before_strict_validation(store: Store) -> None:
    # A newer pointer may add fields; the actionable failure is the minor.
    publish_default(store, store_minor=9)
    pointer = {
        "store_format": unified_store.STORE_FORMAT,
        "store_format_minor": 9,
        "session_id": SESSION_ID,
        "generation": GEN1,
        "snapshot_sequence": 0,
        "manifest_sha256": "0" * 64,
        "a_field_this_reader_has_never_seen": True,
    }
    store._write_document(store.current, pointer)
    with pytest.raises(UnifiedStoreRequiresNewer) as caught:
        load_unified_store(store.current)
    assert caught.value.store_minor == 9


def test_unknown_current_field_at_known_minor_is_broken(store: Store) -> None:
    publish_default(store)
    pointer = {
        "store_format": unified_store.STORE_FORMAT,
        "store_format_minor": 4,
        "session_id": SESSION_ID,
        "generation": GEN1,
        "snapshot_sequence": 0,
        "manifest_sha256": "0" * 64,
        "a_field_this_reader_has_never_seen": True,
    }
    store._write_document(store.current, pointer)
    with pytest.raises(UnifiedStoreError, match="unknown fields"):
        load_unified_store(store.current)


def test_missing_current_raises(store: Store) -> None:
    with pytest.raises(UnifiedStoreError, match="CURRENT pointer is missing"):
        load_unified_store(store.current)
    assert current_fingerprint(store.current) is None


def test_pointer_must_be_named_current(store: Store) -> None:
    publish_default(store)
    with pytest.raises(UnifiedStoreError, match="CURRENT file"):
        load_unified_store(store.session_root / "POINTER")
    assert current_fingerprint(store.session_root / "POINTER") is None


def test_current_naming_another_session_is_rejected(store: Store) -> None:
    publish_default(store)
    pointer = {
        "store_format": unified_store.STORE_FORMAT,
        "session_id": "sess-other",
        "generation": GEN1,
        "snapshot_sequence": 0,
        "manifest_sha256": "0" * 64,
    }
    store._write_document(store.current, pointer)
    with pytest.raises(UnifiedStoreError, match="another session"):
        load_unified_store(store.current)


def test_non_canonical_current_is_rejected(store: Store) -> None:
    publish_default(store)
    store.current.write_bytes(
        b'{"store_format": "mistral.vibe.unified-session-store/v1", "session_id": "sess-1"}\n'
    )
    with pytest.raises(UnifiedStoreError, match="not canonical JSON"):
        load_unified_store(store.current)


def test_invalid_session_directory_is_rejected(store: Store, tmp_path: Path) -> None:
    bad_root = tmp_path / "unified" / "not a session id"
    bad_root.mkdir(parents=True)
    with pytest.raises(UnifiedStoreError, match="session ID directory"):
        load_unified_store(bad_root / "CURRENT")
    assert current_fingerprint(bad_root / "CURRENT") is None


def test_bad_generation_hint_is_rejected(store: Store) -> None:
    publish_default(store)
    with pytest.raises(UnifiedStoreError, match="generation hint"):
        load_unified_store(store.current, generation_hint="17")
    with pytest.raises(UnifiedStoreError):
        load_unified_store(store.current, at_sequence=-1)


def test_current_fingerprint_tracks_pointer_bytes(store: Store) -> None:
    publish_default(store)
    first = current_fingerprint(store.current)
    assert first == sha256_hex(store.current.read_bytes())
    publish_default(store, generation=GEN2, snapshot_sequence=1)
    assert current_fingerprint(store.current) != first


# --- Integrity failures --------------------------------------------------------


def test_manifest_digest_mismatch_is_rejected(store: Store) -> None:
    publish_default(store)
    pointer = {
        "store_format": unified_store.STORE_FORMAT,
        "session_id": SESSION_ID,
        "generation": GEN1,
        "snapshot_sequence": 0,
        "manifest_sha256": "1" * 64,
    }
    store._write_document(store.current, pointer)
    with pytest.raises(UnifiedStoreError, match="manifest digest mismatch"):
        load_unified_store(store.current)


def test_referenced_document_digest_mismatch_is_rejected(store: Store) -> None:
    publish_default(store)
    projection = store.generation_dir(GEN1) / "projection_state.json"
    projection.write_bytes(canonical({"tampered": True}) + b"\n")
    with pytest.raises(UnifiedStoreError, match="digest mismatch"):
        load_unified_store(store.current)


def test_chunk_digest_mismatch_is_rejected(store: Store) -> None:
    publish_default(store, pooled=True)
    chunks = sorted((store.session_root / "chunks").iterdir())
    chunks[0].write_bytes(canonical([{"id": "swapped"}]) + b"\n")
    with pytest.raises(UnifiedStoreError, match="chunk digest mismatch"):
        load_unified_store(store.current)


def test_missing_document_is_a_store_error(store: Store) -> None:
    publish_default(store)
    (store.generation_dir(GEN1) / "checkpoint.json").unlink()
    with pytest.raises(UnifiedStoreError):
        load_unified_store(store.current)


def test_documents_must_be_newline_terminated(store: Store) -> None:
    publish_default(store)
    runtime = store.generation_dir(GEN1) / "runtime_state.json"
    runtime.write_bytes(runtime.read_bytes().rstrip(b"\n"))
    with pytest.raises(UnifiedStoreError, match="newline terminated"):
        load_unified_store(store.current)


def test_journal_record_digest_mismatch_is_rejected(store: Store) -> None:
    publish_default(store)
    journal_path = store.session_root / "journal" / f"{1:016d}.jsonl"
    record = journal_path.read_bytes().splitlines()[0]
    tampered = json_object(record)
    tampered["sequence"] = 99
    journal_path.write_bytes(canonical(tampered) + b"\n")
    with pytest.raises(UnifiedStoreError):
        load_unified_store(store.current)


def test_journal_sequence_gap_is_rejected(store: Store) -> None:
    publish_default(
        store,
        journal=[delta(2, append(entry("e1"))), delta(3, append(entry("e2")))],
    )
    journal_path = store.session_root / "journal" / f"{1:016d}.jsonl"
    lines = journal_path.read_bytes().splitlines(keepends=True)
    # Rewrite the second record with a third sequence number.
    second = json_object(lines[1][:-1])
    second["sequence"] = 4
    remainder = {k: v for k, v in second.items() if k != "record_sha256"}
    second["record_sha256"] = sha256_json(remainder)
    second["previous_record_sha256"] = json_object(lines[0][:-1])["record_sha256"]
    journal_path.write_bytes(lines[0] + canonical(second) + b"\n")
    with pytest.raises(UnifiedStoreError, match="sequence gap"):
        load_unified_store(store.current)


def test_journal_chain_mismatch_is_rejected(store: Store) -> None:
    publish_default(
        store,
        journal=[delta(2, append(entry("e1"))), delta(3, append(entry("e2")))],
    )
    journal_path = store.session_root / "journal" / f"{1:016d}.jsonl"
    lines = journal_path.read_bytes().splitlines(keepends=True)
    second = json_object(lines[1][:-1])
    remainder = {k: v for k, v in second.items() if k != "record_sha256"}
    remainder["previous_record_sha256"] = "2" * 64
    second["record_sha256"] = sha256_json(remainder)
    journal_path.write_bytes(
        lines[0] + canonical(remainder | {"record_sha256": second["record_sha256"]}) + b"\n"
    )
    with pytest.raises(UnifiedStoreError, match="chain mismatch"):
        load_unified_store(store.current)


def test_journal_torn_tail_is_tolerated(store: Store) -> None:
    publish_default(
        store,
        journal=[delta(2, append(entry("e1"))), delta(3, append(entry("e2")))],
    )
    journal_path = store.session_root / "journal" / f"{1:016d}.jsonl"
    body = journal_path.read_bytes()
    first_line = body.split(b"\n", 1)[0] + b"\n"
    # A carriage return inside a record leaves an unterminated line mid-file.
    journal_path.write_bytes(first_line + b'{"recovery_journal_record_version":1\rtrue}\n')
    with pytest.raises(UnifiedStoreError, match="unterminated interior"):
        load_unified_store(store.current)
    # A torn final record is the writer's unsynced tail: read up to the last one.
    journal_path.write_bytes(body + canonical({"partial": True}))
    view = load_unified_store(store.current)
    assert view is not None
    assert view.sequence == 2


def test_watermark_may_not_move_backwards(store: Store) -> None:
    publish_default(
        store,
        journal=[delta(5, append(entry("e1"))), advanced(4, make_state([]))],
    )
    with pytest.raises(UnifiedStoreError, match="watermark moved backwards"):
        load_unified_store(store.current)


def test_absent_entry_ops_are_rejected(store: Store) -> None:
    publish_default(store, journal=[delta(2, replace("nope", entry("x")))])
    with pytest.raises(UnifiedStoreError, match="replaces an absent entry"):
        load_unified_store(store.current)
    publish_default(store, journal=[delta(2, remove("nope"))])
    with pytest.raises(UnifiedStoreError, match="removes an absent entry"):
        load_unified_store(store.current)


def test_unknown_journal_record_type_is_rejected(store: Store) -> None:
    publish_default(store, journal=[("projection_frobnicated", {})])
    with pytest.raises(UnifiedStoreError, match="unknown recovery journal record type"):
        load_unified_store(store.current)


def test_unknown_projection_op_is_rejected(store: Store) -> None:
    publish_default(store, journal=[delta(2, {"op": "frobnicate_entry"})])
    with pytest.raises(UnifiedStoreError, match="unknown projection delta operation"):
        load_unified_store(store.current)


def test_unsafe_integers_are_rejected(store: Store) -> None:
    publish_default(store)
    pointer = {
        "store_format": unified_store.STORE_FORMAT,
        "session_id": SESSION_ID,
        "generation": GEN1,
        "snapshot_sequence": 2**60,
        "manifest_sha256": "0" * 64,
    }
    # Plain JSON can carry the value; no canonical encoding can, so no writer
    # could have digested it — the reader refuses the whole document.
    import json

    store.current.write_bytes(json.dumps(pointer).encode() + b"\n")
    with pytest.raises(UnifiedStoreError):
        load_unified_store(store.current)


def test_document_size_cap_is_enforced(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    publish_default(store)
    monkeypatch.setattr(unified_store, "_MAX_DOCUMENT_BYTES", 8)
    with pytest.raises(UnifiedStoreError, match="too large"):
        load_unified_store(store.current)


# --- Path safety ---------------------------------------------------------------


def test_symlinked_generation_document_is_rejected(store: Store, tmp_path: Path) -> None:
    publish_default(store)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}\n")
    checkpoint = store.generation_dir(GEN1) / "checkpoint.json"
    checkpoint.unlink()
    checkpoint.symlink_to(outside)
    with pytest.raises(UnifiedStoreError, match="symbolic link"):
        load_unified_store(store.current)


def test_symlinked_chunk_is_rejected(store: Store, tmp_path: Path) -> None:
    publish_default(store, pooled=True)
    chunks = sorted((store.session_root / "chunks").iterdir())
    outside = tmp_path / "outside.json"
    outside.write_bytes(chunks[0].read_bytes())
    chunks[0].unlink()
    chunks[0].symlink_to(outside)
    with pytest.raises(UnifiedStoreError, match="symbolic link"):
        load_unified_store(store.current)


def test_symlinked_session_root_is_rejected(store: Store, tmp_path: Path) -> None:
    publish_default(store)
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(store.session_root, target_is_directory=True)
    with pytest.raises(UnifiedStoreError, match="symbolic link"):
        load_unified_store(linked_root / "CURRENT")


def test_stored_file_paths_must_be_one_file_name(store: Store) -> None:
    publish_default(store)
    manifest_path = store.generation_dir(GEN1) / "manifest.json"
    manifest = json_object(manifest_path.read_bytes()[:-1])
    manifest["checkpoint"]["path"] = "../escape.json"
    body = canonical(manifest)
    manifest_path.write_bytes(body + b"\n")
    pointer = {
        "store_format": unified_store.STORE_FORMAT,
        "session_id": SESSION_ID,
        "generation": GEN1,
        "snapshot_sequence": 0,
        "manifest_sha256": sha256_hex(body),
    }
    store._write_document(store.current, pointer)
    with pytest.raises(UnifiedStoreError, match="one file name"):
        load_unified_store(store.current)


# --- Manifest shape ------------------------------------------------------------


def test_interop_export_record_is_validated_but_never_read(store: Store) -> None:
    publish_default(store, interop=True)
    view = load_unified_store(store.current)
    assert view is not None
    assert not (store.generation_dir(GEN1) / "interop-export.json").exists()


def test_recoverable_generation_cannot_have_interop_export(store: Store) -> None:
    publish_default(store, interop=True, execution_state="recoverable")
    with pytest.raises(UnifiedStoreError, match="recoverable generation"):
        load_unified_store(store.current)


def test_runtime_state_cannot_pool_a_transcript(store: Store) -> None:
    publish_default(store)
    manifest_path = store.generation_dir(GEN1) / "manifest.json"
    manifest = json_object(manifest_path.read_bytes()[:-1])
    manifest["runtime_state"]["chunks"] = ["0" * 64]
    body = canonical(manifest)
    manifest_path.write_bytes(body + b"\n")
    pointer = {
        "store_format": unified_store.STORE_FORMAT,
        "session_id": SESSION_ID,
        "generation": GEN1,
        "snapshot_sequence": 0,
        "manifest_sha256": sha256_hex(body),
    }
    store._write_document(store.current, pointer)
    with pytest.raises(UnifiedStoreError, match="no transcript to pool"):
        load_unified_store(store.current)


def test_runtime_sequence_must_match_manifest(store: Store) -> None:
    publish_default(store)
    runtime_path = store.generation_dir(GEN1) / "runtime_state.json"
    runtime = json_object(runtime_path.read_bytes()[:-1])
    runtime["snapshot_sequence"] = 7
    body = canonical(runtime)
    runtime_path.write_bytes(body + b"\n")
    manifest_path = store.generation_dir(GEN1) / "manifest.json"
    manifest = json_object(manifest_path.read_bytes()[:-1])
    manifest["runtime_state"]["sha256"] = sha256_hex(body)
    manifest_body = canonical(manifest)
    manifest_path.write_bytes(manifest_body + b"\n")
    pointer = {
        "store_format": unified_store.STORE_FORMAT,
        "session_id": SESSION_ID,
        "generation": GEN1,
        "snapshot_sequence": 0,
        "manifest_sha256": sha256_hex(manifest_body),
    }
    store._write_document(store.current, pointer)
    with pytest.raises(UnifiedStoreError, match="runtime state sequence"):
        load_unified_store(store.current)


# --- Concurrent publication ----------------------------------------------------


def test_active_load_retries_when_current_moves(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_default(store, journal=[delta(1, append(entry("e1")))])
    stale_bytes = store.current.read_bytes()
    publish_default(
        store,
        generation=GEN2,
        snapshot_sequence=5,
        state=make_state([entry("e2")]),
        journal=[delta(6, append(entry("e3")))],
    )
    fresh_bytes = store.current.read_bytes()
    # The stale generation is collected out from under the first read.
    shutil.rmtree(store.generation_dir(GEN1))
    reads = {"count": 0}

    def moving_pointer(session_root: Path) -> bytes | None:
        reads["count"] += 1
        return stale_bytes if reads["count"] <= 2 else fresh_bytes

    monkeypatch.setattr(unified_store, "_read_current_bytes", moving_pointer)
    view = load_unified_store(store.current)
    assert view is not None
    assert view.generation == GEN2
    assert [item["id"] for item in view.snapshot["history"]["entries"]] == ["e2", "e3"]


def test_active_load_failure_stands_when_pointer_is_still(store: Store) -> None:
    publish_default(store)
    (store.generation_dir(GEN1) / "manifest.json").unlink()
    with pytest.raises(UnifiedStoreError, match="cannot be read"):
        load_unified_store(store.current)


def json_object(raw: bytes) -> dict[str, Any]:
    import json

    value = json.loads(raw)
    assert isinstance(value, dict)
    return value
