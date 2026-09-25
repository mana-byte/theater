"""Canonical document and chunk I/O for the Vibe unified-session store."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import rfc8785

from .unified_store_types import (
    _CANONICAL_INTEGER_RANGE,
    _MAX_DOCUMENT_BYTES,
    _READER_CHUNK_CACHE_BYTES,
    UnifiedStoreError,
    _StoredFile,
)

# --- Canonical JSON and digests ------------------------------------------------


def _canonical_json(value: Any) -> bytes:
    """The RFC 8785 encoding of ``value``, matching the reference writer.

    Falls back to ``rfc8785`` where json differs; unencodable values cannot have been digested.
    """
    if _standard_encoder_is_canonical(value):
        try:
            return json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        except (RecursionError, UnicodeEncodeError):
            pass
    try:
        return rfc8785.dumps(value)
    except Exception as exc:
        raise UnifiedStoreError(f"a stored value has no canonical JSON encoding: {exc}") from exc


def _standard_encoder_is_canonical(value: Any) -> bool:
    pending: list[Any] = [value]
    while pending:
        item = pending.pop()
        item_type = type(item)
        if item_type is str or item is None or item_type is bool:
            continue
        if item_type is dict:
            for key, nested in item.items():
                if type(key) is not str or not key.isascii():
                    return False
                pending.append(nested)
            continue
        if item_type is list:
            pending.extend(item)
            continue
        if item_type is int:
            if item not in _CANONICAL_INTEGER_RANGE:
                return False
            continue
        return False
    return True


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256(_canonical_json(value))


# --- Document reads -----------------------------------------------------------


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise UnifiedStoreError(f"stored path cannot be a symbolic link: {path}")


def _reject_symlink_components(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise UnifiedStoreError(f"stored path escapes its configured root: {path}") from exc
    current = root
    _reject_symlink(current)
    for part in relative.parts:
        current = current / part
        _reject_symlink(current)


def _read_document_body(path: Path, description: str) -> bytes:
    _reject_symlink(path)
    with path.open("rb") as stream:
        data = stream.read(_MAX_DOCUMENT_BYTES + 1)
        if len(data) > _MAX_DOCUMENT_BYTES:
            raise UnifiedStoreError(f"stored JSON document is too large: {description}")
    if not data.endswith(b"\n"):
        raise UnifiedStoreError(f"stored JSON document is not newline terminated: {description}")
    return data[:-1]


def _decode_json(body: bytes, description: str) -> Any:
    try:
        return json.loads(body)
    except (RecursionError, ValueError) as exc:
        raise UnifiedStoreError(f"stored JSON document is not valid JSON: {description}") from exc


def _decode_canonical_document(body: bytes, description: str) -> Any:
    value = _decode_json(body, description)
    if body != _canonical_json(value):
        raise UnifiedStoreError(f"stored JSON document is not canonical JSON: {description}")
    return value


def _read_canonical_document(path: Path, description: str) -> tuple[Any, bytes]:
    body = _read_document_body(path, description)
    return _decode_canonical_document(body, description), body


def _read_referenced_document(generation_dir: Path, descriptor: _StoredFile) -> Any:
    body = _read_document_body(generation_dir / descriptor.path, descriptor.path)
    if _sha256(body) != descriptor.sha256:
        raise UnifiedStoreError(f"stored record digest mismatch: {descriptor.path}")
    return _decode_json(body, descriptor.path)


class _ChunkCache:
    """Thread-safe bounded LRU for digest-verified chunk bodies."""

    def __init__(self, budget: int = _READER_CHUNK_CACHE_BYTES) -> None:
        if budget <= 0:
            raise UnifiedStoreError("chunk cache budget must be positive")
        self._budget = budget
        self._bodies: OrderedDict[str, bytes] = OrderedDict()
        self._resident = 0
        self._lock = threading.Lock()

    @property
    def resident_bytes(self) -> int:
        with self._lock:
            return self._resident

    def read(self, chunk_root: Path, digest: str) -> bytes:
        path = chunk_root / f"{digest}.json"
        _reject_symlink_components(chunk_root.parent, path)
        with self._lock:
            cached = self._bodies.get(digest)
            if cached is not None:
                self._bodies.move_to_end(digest)
                return cached
        body = _read_document_body(path, f"chunk {digest}")
        if _sha256(body) != digest:
            raise UnifiedStoreError(f"stored chunk digest mismatch: {digest}")
        if len(body) <= self._budget:
            with self._lock:
                if digest in self._bodies:
                    self._bodies.move_to_end(digest)
                else:
                    self._bodies[digest] = body
                    self._resident += len(body)
                while self._resident > self._budget:
                    self._resident -= len(self._bodies.popitem(last=False)[1])
        return body

    def clear(self) -> None:
        with self._lock:
            self._bodies.clear()
            self._resident = 0


def _read_chunked_transcript(
    chunk_root: Path, digests: tuple[str, ...], chunk_cache: _ChunkCache | None = None
) -> list[Any]:
    items: list[Any] = []
    total_bytes = 0
    for digest in digests:
        if chunk_cache is not None:
            body = chunk_cache.read(chunk_root, digest)
        else:
            path = chunk_root / f"{digest}.json"
            _reject_symlink(path)
            body = _read_document_body(path, path.name)
            if _sha256(body) != digest:
                raise UnifiedStoreError(f"stored chunk digest mismatch: {digest}")
        total_bytes += len(body)
        if total_bytes > _MAX_DOCUMENT_BYTES:
            raise UnifiedStoreError("stored chunked transcript is too large")
        chunk = _decode_json(body, f"chunk {digest}")
        if not isinstance(chunk, list):
            raise UnifiedStoreError(f"stored chunk is not a transcript run: {digest}")
        items.extend(chunk)
    return items


def _attach_transcript(document: dict[str, Any], path: tuple[str, ...], items: list[Any]) -> None:
    node: Any = document
    for key in path[:-1]:
        if not isinstance(node, dict):
            raise UnifiedStoreError("chunked document is missing its transcript container")
        node = node.get(key)
    if not isinstance(node, dict) or node.get(path[-1]) != []:
        raise UnifiedStoreError("chunked document envelope must hold an empty transcript")
    node[path[-1]] = items
