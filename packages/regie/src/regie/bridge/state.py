"""Private durable bridge identity, receipts, and exclusive process lock."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import IO, Any

_STATE_VERSION = 1


class BridgeStateError(RuntimeError):
    """Durable bridge state is invalid or inaccessible."""


class BridgeAlreadyRunning(BridgeStateError):
    """Another process owns this bridge state directory."""


@dataclass(frozen=True, slots=True, repr=False)
class DurableBridgeState:
    provider_credential: str
    registration_key: str
    provider_id: str | None = None
    tmux_server_identity: str | None = None
    report_revision: int = 0
    presence_revision: int = 0


@dataclass(frozen=True, slots=True)
class DurableLaunchIntent:
    operation_id: str
    launch_id: str
    request_digest: str
    provider_id: str
    provider_generation: int
    participant_id: str
    executable: str
    tmux_server_identity: str | None
    terminal_incarnation: str
    provisional_window_name: str
    dispatched: bool = False


class BridgeStateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_path = state_dir / "bridge-state.json"
        self.lock_path = state_dir / "bridge.lock"
        self.receipt_dir = state_dir / "receipts"
        self.launch_dir = state_dir / "launches"
        self._lock_file: IO[str] | None = None
        self._state: DurableBridgeState | None = None

    @property
    def state(self) -> DurableBridgeState:
        if self._state is None:
            raise BridgeStateError("bridge state has not been opened")
        return self._state

    def acquire(self) -> DurableBridgeState:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        lock_file = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise BridgeAlreadyRunning(
                "another Régie tmux bridge owns this state directory"
            ) from None
        os.fchmod(lock_file.fileno(), 0o600)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"{os.getpid()}\n")
        lock_file.flush()
        os.fsync(lock_file.fileno())
        self._lock_file = lock_file
        try:
            self.receipt_dir.mkdir(mode=0o700, exist_ok=True)
            self.receipt_dir.chmod(0o700)
            self.launch_dir.mkdir(mode=0o700, exist_ok=True)
            self.launch_dir.chmod(0o700)
            self._state = self._read_or_create()
        except BaseException:
            self.release()
            raise
        return self._state

    def release(self) -> None:
        lock_file, self._lock_file = self._lock_file, None
        if lock_file is None:
            return
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def update(self, **changes: Any) -> DurableBridgeState:
        state = replace(self.state, **changes)
        self._write_state(state)
        self._state = state
        return state

    def next_report_revision(self) -> int:
        revision = self.state.report_revision + 1
        self.update(report_revision=revision)
        return revision

    def next_presence_revision(self) -> int:
        revision = self.state.presence_revision + 1
        self.update(presence_revision=revision)
        return revision

    def write_receipt(self, method: str, operation_id: str, result: dict[str, object]) -> None:
        name = _receipt_name(method, operation_id)
        self._write_json(
            self.receipt_dir / name,
            {"method": method, "operation_id": operation_id, "result": result},
        )

    def receipt(self, method: str, operation_id: str) -> dict[str, object] | None:
        path = self.receipt_dir / _receipt_name(method, operation_id)
        if not path.exists():
            return None
        value = self._read_json(path)
        result = value.get("result")
        return dict(result) if isinstance(result, dict) else None

    def receipts(self, *, limit: int = 500) -> tuple[dict[str, object], ...]:
        paths = sorted(
            self.receipt_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True
        )[:limit]
        receipts: list[dict[str, object]] = []
        for path in paths:
            value = self._read_json(path)
            method = value.get("method")
            operation_id = value.get("operation_id")
            result = value.get("result")
            if not isinstance(method, str) or not isinstance(operation_id, str):
                raise BridgeStateError(f"invalid bridge receipt {path.name}")
            if not isinstance(result, dict):
                raise BridgeStateError(f"invalid bridge receipt {path.name}")
            receipt = {"method": method, "operation_id": operation_id, **result}
            receipts.append(receipt)
        return tuple(receipts)

    def clear_receipts(self) -> None:
        for path in self.receipt_dir.glob("*.json"):
            path.unlink()

    def acknowledge_receipts(
        self, receipts: tuple[dict[str, object], ...], *, ignored_operation_ids: object = ()
    ) -> None:
        if not isinstance(ignored_operation_ids, (tuple, list)) or not all(
            isinstance(item, str) for item in ignored_operation_ids
        ):
            raise BridgeStateError("invalid ignored receipt ids; preserving receipts for retry")
        ignored = set(ignored_operation_ids)
        for receipt in receipts:
            method = receipt.get("method")
            operation_id = receipt.get("operation_id")
            if not isinstance(method, str) or not isinstance(operation_id, str):
                continue
            path = self.receipt_dir / _receipt_name(method, operation_id)
            if not path.exists():
                continue
            stored = self._read_json(path)
            result = {key: value for key, value in receipt.items() if key != "method"}
            if stored == {"method": method, "operation_id": operation_id, "result": result}:
                if operation_id in ignored:
                    quarantine = self.state_dir / "unmatched-receipts"
                    quarantine.mkdir(mode=0o700, exist_ok=True)
                    quarantine.chmod(0o700)
                    self._write_json(quarantine / path.name, stored)
                path.unlink()
        _sync_directory(self.receipt_dir)

    def prepare_launch(
        self,
        launch_id: str,
        request_digest: str,
        *,
        operation_id: str,
        provider_id: str,
        provider_generation: int,
        participant_id: str,
        executable: str,
        tmux_server_identity: str,
    ) -> DurableLaunchIntent:
        path = self.launch_dir / _launch_name(launch_id)
        if path.exists():
            intent = self._read_launch(path)
            if (
                intent.launch_id != launch_id
                or intent.request_digest != request_digest
                or intent.operation_id != operation_id
                or intent.provider_id != provider_id
                or intent.provider_generation != provider_generation
                or intent.participant_id != participant_id
                or intent.executable != executable
                or intent.tmux_server_identity != tmux_server_identity
            ):
                raise BridgeStateError("launch identity was reused with different execution facts")
            return intent
        intent = DurableLaunchIntent(
            operation_id=operation_id,
            launch_id=launch_id,
            request_digest=request_digest,
            provider_id=provider_id,
            provider_generation=provider_generation,
            participant_id=participant_id,
            executable=executable,
            tmux_server_identity=tmux_server_identity,
            terminal_incarnation=f"tmux-{secrets.token_urlsafe(24)}",
            provisional_window_name=f"regie-launch-{secrets.token_urlsafe(18)}",
        )
        self._write_launch(path, intent)
        return intent

    def launch_intents(self) -> tuple[DurableLaunchIntent, ...]:
        return tuple(self._read_launch(path) for path in sorted(self.launch_dir.glob("*.json")))

    def mark_launch_dispatched(self, intent: DurableLaunchIntent) -> DurableLaunchIntent:
        path = self.launch_dir / _launch_name(intent.launch_id)
        current = self._read_launch(path)
        if current != intent:
            raise BridgeStateError("launch intent changed before terminal dispatch")
        if current.dispatched:
            return current
        dispatched = replace(current, dispatched=True)
        self._write_launch(path, dispatched)
        return dispatched

    def complete_launch(self, intent: DurableLaunchIntent) -> None:
        path = self.launch_dir / _launch_name(intent.launch_id)
        current = self._read_launch(path)
        if current.launch_id != intent.launch_id or current.request_digest != intent.request_digest:
            raise BridgeStateError("launch intent changed before completion")
        path.unlink()
        _sync_directory(path.parent)

    def _read_or_create(self) -> DurableBridgeState:
        if not self.state_path.exists():
            state = DurableBridgeState(
                provider_credential=f"regie-provider-v1.{secrets.token_urlsafe(12)}."
                f"{secrets.token_urlsafe(32)}",
                registration_key=f"register-{secrets.token_urlsafe(24)}",
            )
            self._write_state(state)
            return state
        value = self._read_json(self.state_path)
        if value.get("version") != _STATE_VERSION:
            raise BridgeStateError("bridge state version is unsupported")
        try:
            state = DurableBridgeState(
                provider_credential=_required_string(value, "provider_credential"),
                registration_key=_required_string(value, "registration_key"),
                provider_id=_optional_string(value, "provider_id"),
                tmux_server_identity=_optional_string(value, "tmux_server_identity"),
                report_revision=_nonnegative_integer(value, "report_revision"),
                presence_revision=_nonnegative_integer(value, "presence_revision"),
            )
        except (TypeError, ValueError) as exc:
            raise BridgeStateError(f"bridge state is invalid: {exc}") from exc
        self.state_path.chmod(0o600)
        return state

    def _write_state(self, state: DurableBridgeState) -> None:
        self._write_json(self.state_path, {"version": _STATE_VERSION, **asdict(state)})

    def _read_launch(self, path: Path) -> DurableLaunchIntent:
        value = self._read_json(path)
        if value.get("version") != _STATE_VERSION:
            raise BridgeStateError("launch intent has an unsupported version")
        try:
            return DurableLaunchIntent(
                operation_id=_required_string(value, "operation_id"),
                launch_id=_required_string(value, "launch_id"),
                request_digest=_required_string(value, "request_digest"),
                provider_id=_required_string(value, "provider_id"),
                provider_generation=_nonnegative_integer(value, "provider_generation"),
                participant_id=_required_string(value, "participant_id"),
                executable=_required_string(value, "executable"),
                tmux_server_identity=_optional_string(value, "tmux_server_identity"),
                terminal_incarnation=_required_string(value, "terminal_incarnation"),
                provisional_window_name=_required_string(value, "provisional_window_name"),
                dispatched=_boolean(value, "dispatched"),
            )
        except (TypeError, ValueError) as exc:
            raise BridgeStateError(f"launch intent is invalid: {exc}") from exc

    def _write_launch(self, path: Path, intent: DurableLaunchIntent) -> None:
        self._write_json(path, {"version": _STATE_VERSION, **asdict(intent)})

    def _write_json(self, path: Path, value: dict[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, allow_nan=False, separators=(",", ":"), sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
            path.chmod(0o600)
            _sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeStateError(
                f"could not read private bridge state {path.name}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise BridgeStateError(f"private bridge state {path.name} is not an object")
        return value


def _receipt_name(method: str, operation_id: str) -> str:
    import hashlib

    digest = hashlib.sha256(f"{method}\0{operation_id}".encode()).hexdigest()
    return f"{digest}.json"


def _launch_name(launch_id: str) -> str:
    import hashlib

    return f"{hashlib.sha256(launch_id.encode()).hexdigest()}.json"


def _required_string(value: dict[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{name} must be a non-empty string")
    return item


def _optional_string(value: dict[str, object], name: str) -> str | None:
    item = value.get(name)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise ValueError(f"{name} must be a non-empty string or null")
    return item


def _nonnegative_integer(value: dict[str, object], name: str) -> int:
    item = value.get(name, 0)
    if type(item) is not int or item < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return item


def _boolean(value: dict[str, object], name: str) -> bool:
    item = value.get(name, False)
    if type(item) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return item


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "BridgeAlreadyRunning",
    "BridgeStateError",
    "BridgeStateStore",
    "DurableBridgeState",
    "DurableLaunchIntent",
]
