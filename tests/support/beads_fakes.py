"""In-memory fakes for the Beads MCP plugin: play client, board and job facts.

``FakeClient`` stands in for ``TheaterPluginClient`` with the same play-context
shape; ``FakeBoard`` subclasses the real ``Board`` and emulates the bd update
semantics the plugin relies on (per-key ``--metadata`` merges, scalar-only
``--set-metadata`` parsing, native ``closed_at`` on close) from a JSON store,
so boards persist across one service's per-operation instances.
"""

from __future__ import annotations

import contextvars
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from theater.mcp_plugins.builtin.beads.backend import Board

caller = contextvars.ContextVar("beads_test_caller", default="author")
instance = contextvars.ContextVar("beads_test_instance", default="instance-one")


def fact(handle: str, state: str = "done", /, **overrides: Any) -> dict[str, Any]:
    """One producer-shaped job fact; overrides tweak or drop individual fields."""
    payload: dict[str, Any] = {
        "handle": handle,
        "available": True,
        "state": state,
        "error_code": None,
        "kind": "work",
        "caller_id": "author",
        "target_id": "target-one",
        "created_at": time.time(),
        "finished_at": time.time(),
        "contract_preset": "delegate",
        "structured_status": "parsed",
        "delegate_status": "done",
        "review_verdict": None,
        "git": {"pin_state": "pinned", "commit_sha": "abc123", "dirty_entries": 0, "clean": True},
        "handoff": None,
        "play": {"instance_id": "instance-one", "caller_seated_at_creation": True},
    }
    for key, value in overrides.items():
        if key == "absent":
            payload.pop(value, None)
        else:
            payload[key] = value
    return payload


class FakeClient:
    """The daemon's play context, membership and job-facts surface."""

    enabled = True
    permissions: ClassVar[list[str]] = ["read", "write"]
    members: ClassVar[list[dict[str, str]]] = [
        {"participant_id": "author", "name": "Author", "position": "master"},
        {"participant_id": "peer", "name": "Peer", "position": "member"},
    ]
    live: ClassVar[dict[str, bool]] = {"instance-one": True, "instance-two": True}
    unavailable = False
    facts: ClassVar[dict[str, dict[str, Any]]] = {}
    facts_calls = 0

    @classmethod
    def reset(cls) -> None:
        """Restore every class default; tests mutate this shared state."""
        cls.enabled = True
        cls.permissions = ["read", "write"]
        cls.members = [
            {"participant_id": "author", "name": "Author", "position": "master"},
            {"participant_id": "peer", "name": "Peer", "position": "member"},
        ]
        cls.live = {"instance-one": True, "instance-two": True}
        cls.unavailable = False
        cls.facts = {}
        cls.facts_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def play_context(self):
        return {
            "participant_id": caller.get(),
            "plugin_name": "beads",
            "enabled": self.enabled,
            "play": {
                "instance_id": instance.get(),
                "master_id": "author",
                "name": "hierarchy",
                "position": "master",
                "plugins": ["beads"],
                "permissions": self.permissions,
            },
        }

    async def play_members(self):
        return self.members

    async def play_instances_status(self, instance_ids):
        if self.unavailable:
            raise OSError("daemon unavailable")
        return [{"instance_id": key, "active": self.live.get(key, False)} for key in instance_ids]

    async def get_job_facts(self, handles):
        assert len(handles) <= 50
        assert all(handle.strip() for handle in handles)
        type(self).facts_calls += 1
        results = []
        for handle in handles:
            known = self.facts.get(handle)
            results.append(known if known is not None else {"handle": handle, "available": False})
        return results


_VALUE_FLAGS = frozenset(
    {
        "--description",
        "--type",
        "--priority",
        "--status",
        "--assignee",
        "--parent",
        "--deps",
        "--append-notes",
        "--metadata",
        "--set-metadata",
        "--unset-metadata",
    }
)
_BARE_FLAGS = frozenset({"--force"})


def _parse(args: tuple[str, ...]) -> tuple[list[str], dict[str, list[str]]]:
    positionals: list[str] = []
    flags: dict[str, list[str]] = {}
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if not token.startswith("--"):
            positionals.append(token)
        elif "=" in token:
            key, _, value = token.partition("=")
            flags.setdefault(key, []).append(value)
        elif token in _BARE_FLAGS:
            flags.setdefault(token, []).append("")
        elif token not in _VALUE_FLAGS:
            raise AssertionError(f"unmocked bd flag {token!r}")
        elif index >= len(args):
            raise AssertionError(f"bd flag {token} without a value")
        else:
            flags.setdefault(token, []).append(args[index])
            index += 1
    return positionals, flags


class FakeBoard(Board):
    """A Board emulating bd's update semantics from a JSON store on disk.

    Like the real bd, every invocation reads the store fresh: an operation's
    board instance never serves a snapshot taken before it acquired the lock.
    """

    def __init__(self, state_dir: Path, instance_id: str, bd_path: str | None = None) -> None:
        super().__init__(state_dir, instance_id, bd_path or "unused-bd")
        self.store = self.path / "fake_board.json"
        self.rows_by_id: dict[str, dict[str, Any]] = {}
        self.ready_ids_by_id: set[str] = set()
        self.next_serial = 1

    def ensure(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        if self.store.is_file():
            saved = json.loads(self.store.read_text())
            self.rows_by_id = saved["rows"]
            self.ready_ids_by_id = set(saved["ready"])
            self.next_serial = saved["next_serial"]

    def _save(self) -> None:
        self.ensure()
        self.store.write_text(
            json.dumps(
                {
                    "rows": self.rows_by_id,
                    "ready": sorted(self.ready_ids_by_id),
                    "next_serial": self.next_serial,
                }
            )
        )

    def rows(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self.rows_by_id

    def ready_ids(self) -> set[str]:
        self._load()
        return set(self.ready_ids_by_id)

    def command(self, *args: str, actor: str) -> Any:
        self._load()
        positionals, flags = _parse(args)
        verb = positionals[0]
        if verb == "create":
            return self._create(flags)
        if verb == "update":
            self._update(positionals[1], flags)
            return None
        if verb == "dep":
            self._dep(positionals, flags)
            return None
        raise AssertionError(f"unmocked bd verb {verb!r}")

    def _create(self, flags: dict[str, list[str]]) -> dict[str, Any]:
        task_id = f"{self.prefix}-{self.next_serial:04x}"
        self.next_serial += 1
        row: dict[str, Any] = {
            "id": task_id,
            "title": flags.get("--title", [""])[0],
            "description": flags.get("--description", [""])[0],
            "issue_type": flags.get("--type", ["task"])[0],
            "status": "open",
            "priority": flags.get("--priority", ["2"])[0],
            "assignee": flags.get("--assignee", [None])[0],
            "parent": flags.get("--parent", [None])[0],
            "dependencies": [],
            "metadata": {},
            "notes": "",
        }
        for dependency in flags.get("--deps", [""])[0].split(","):
            if dependency:
                row["dependencies"].append({"depends_on_id": dependency, "type": "blocks"})
        self.rows_by_id[task_id] = row
        self._save()
        return {"id": task_id}

    def _update(self, task_id: str, flags: dict[str, list[str]]) -> None:
        row = self.rows_by_id[task_id]
        for key in ("--title", "--description", "--type", "--priority"):
            if key in flags:
                row[key[2:]] = flags[key][-1]
        if "--append-notes" in flags:
            addition = flags["--append-notes"][-1]
            row["notes"] = f"{row['notes']}\n\n{addition}" if row["notes"] else addition
        if "--assignee" in flags:
            row["assignee"] = flags["--assignee"][-1]
        if "--parent" in flags:
            row["parent"] = flags["--parent"][-1] or None
        self._apply_metadata(row, flags)
        if "--status" in flags:
            self._apply_status(row, flags["--status"][-1])
        if "--force" in flags:
            assert flags["--force"] == [""]
        self._save()

    @staticmethod
    def _apply_metadata(row: dict[str, Any], flags: dict[str, list[str]]) -> None:
        for blob in flags.get("--metadata", []):
            try:
                parsed = json.loads(blob)
            except ValueError as exc:
                raise AssertionError("fake board got an invalid metadata blob") from exc
            if not isinstance(parsed, dict):
                raise TypeError("fake board only merges object metadata")
            row["metadata"].update(parsed)
        for entry in flags.get("--set-metadata", []):
            key, _, value = entry.partition("=")
            try:
                parsed = json.loads(value)
            except ValueError:
                row["metadata"][key] = value
                continue
            # bd parses scalar metadata but keeps arrays and objects as text.
            row["metadata"][key] = value if isinstance(parsed, (dict, list)) else parsed
        for key in flags.get("--unset-metadata", []):
            row["metadata"].pop(key, None)

    @staticmethod
    def _apply_status(row: dict[str, Any], status: str) -> None:
        row["status"] = status
        if status == "closed":
            now = datetime.now(UTC)
            row["closed_at"] = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        else:
            row.pop("closed_at", None)

    def _dep(self, positionals: list[str], _flags: dict[str, list[str]]) -> None:
        action, task_id, prerequisite_id = positionals[1], positionals[2], positionals[3]
        row = self.rows_by_id[task_id]
        if action == "add":
            row["dependencies"].append({"depends_on_id": prerequisite_id, "type": "blocks"})
        else:
            row["dependencies"] = [
                edge for edge in row["dependencies"] if edge["depends_on_id"] != prerequisite_id
            ]
        self._save()
