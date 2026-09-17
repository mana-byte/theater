"""Workspace ownership, usage, and deletion-fence values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from theater.frontend.dto._wire import (
    JSONValue,
    append_extras,
    extras,
    integer_value,
    object_value,
    string_value,
)


class WorkspaceOwnershipKind(StrEnum):
    THEATER = "theater"
    FRONTEND = "frontend"
    BORROWED = "borrowed"


class WorkspaceUsageHolderKind(StrEnum):
    RESERVATION = "reservation"
    PARTICIPANT = "participant"


class WorkspaceState(StrEnum):
    ACTIVE = "active"
    DELETING = "deleting"
    REMOVED = "removed"
    RECONCILE = "reconcile"


@dataclass(frozen=True, slots=True)
class WorkspaceUsage:
    workspace_id: str
    holder_kind: str
    holder_id: str
    acquired_at: float
    released_at: float | None = None
    release_reason: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> WorkspaceUsage:
        data = object_value(value, "workspace usage")
        return cls(
            workspace_id=string_value(data.get("workspace_id"), "usage.workspace_id") or "",
            holder_kind=string_value(data.get("holder_kind"), "usage.holder_kind") or "",
            holder_id=string_value(data.get("holder_id"), "usage.holder_id") or "",
            acquired_at=_number(data.get("acquired_at"), "usage.acquired_at"),
            released_at=_optional_number(data.get("released_at"), "usage.released_at"),
            release_reason=string_value(
                data.get("release_reason"), "usage.release_reason", optional=True
            ),
            extra=extras(
                data,
                {
                    "workspace_id",
                    "holder_kind",
                    "holder_id",
                    "acquired_at",
                    "released_at",
                    "release_reason",
                },
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "workspace_id": self.workspace_id,
                "holder_kind": self.holder_kind,
                "holder_id": self.holder_id,
                "acquired_at": self.acquired_at,
                "released_at": self.released_at,
                "release_reason": self.release_reason,
            },
            self.extra,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceUsageHandoff:
    workspace_id: str
    reservation_id: str
    participant_id: str

    def to_wire(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "reservation_id": self.reservation_id,
            "participant_id": self.participant_id,
        }

    @classmethod
    def from_wire(cls, value: object) -> WorkspaceUsageHandoff:
        data = object_value(value, "workspace usage handoff")
        return cls(
            workspace_id=string_value(data.get("workspace_id"), "handoff.workspace_id") or "",
            reservation_id=string_value(data.get("reservation_id"), "handoff.reservation_id") or "",
            participant_id=string_value(data.get("participant_id"), "handoff.participant_id") or "",
        )


@dataclass(frozen=True, slots=True)
class WorkspaceDeletionFence:
    token: str
    revision: int
    owner_id: str
    operation_id: str | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "token": self.token,
            "revision": self.revision,
            "owner_id": self.owner_id,
            "operation_id": self.operation_id,
        }

    @classmethod
    def from_wire(cls, value: object) -> WorkspaceDeletionFence:
        return _fence_from_wire(value)


@dataclass(frozen=True, slots=True)
class Workspace:
    workspace_id: str
    ownership_kind: str
    owner_id: str
    path: str
    canonical_repository_root: str | None
    resolved_base_commit: str | None
    branch: str | None
    state: str
    usages: tuple[WorkspaceUsage, ...] = ()
    deletion_fence: WorkspaceDeletionFence | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "workspace_id": self.workspace_id,
                "ownership_kind": self.ownership_kind,
                "owner_id": self.owner_id,
                "path": self.path,
                "canonical_repository_root": self.canonical_repository_root,
                "resolved_base_commit": self.resolved_base_commit,
                "branch": self.branch,
                "state": self.state,
                "usages": [usage.to_wire() for usage in self.usages],
                "deletion_fence": (
                    self.deletion_fence.to_wire() if self.deletion_fence is not None else None
                ),
            },
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> Workspace:
        data = object_value(value, "workspace")
        raw_usages = data.get("usages", [])
        if not isinstance(raw_usages, (list, tuple)):
            raise TypeError("workspace.usages must be an array")
        fence_value = data.get("deletion_fence")
        fence = _fence_from_wire(fence_value) if fence_value is not None else None
        return cls(
            workspace_id=string_value(data.get("workspace_id"), "workspace.workspace_id") or "",
            ownership_kind=string_value(data.get("ownership_kind"), "workspace.ownership_kind")
            or "",
            owner_id=string_value(data.get("owner_id"), "workspace.owner_id") or "",
            path=string_value(data.get("path"), "workspace.path") or "",
            canonical_repository_root=string_value(
                data.get("canonical_repository_root"),
                "workspace.canonical_repository_root",
                optional=True,
            ),
            resolved_base_commit=string_value(
                data.get("resolved_base_commit"), "workspace.resolved_base_commit", optional=True
            ),
            branch=string_value(data.get("branch"), "workspace.branch", optional=True),
            state=string_value(data.get("state"), "workspace.state") or "",
            usages=tuple(WorkspaceUsage.from_wire(item) for item in raw_usages),
            deletion_fence=fence,
            extra=extras(
                data,
                {
                    "workspace_id",
                    "ownership_kind",
                    "owner_id",
                    "path",
                    "canonical_repository_root",
                    "resolved_base_commit",
                    "branch",
                    "state",
                    "usages",
                    "deletion_fence",
                },
            ),
        )


def _fence_from_wire(value: object) -> WorkspaceDeletionFence:
    data = object_value(value, "workspace deletion fence")
    return WorkspaceDeletionFence(
        token=string_value(data.get("token"), "deletion fence.token") or "",
        revision=integer_value(data.get("revision"), "deletion fence.revision"),
        owner_id=string_value(data.get("owner_id"), "deletion fence.owner_id") or "",
        operation_id=string_value(
            data.get("operation_id"), "deletion fence.operation_id", optional=True
        ),
    )


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{label} must be a number")
    return float(value)


def _optional_number(value: object, label: str) -> float | None:
    return None if value is None else _number(value, label)


__all__ = [
    "Workspace",
    "WorkspaceDeletionFence",
    "WorkspaceOwnershipKind",
    "WorkspaceState",
    "WorkspaceUsage",
    "WorkspaceUsageHandoff",
    "WorkspaceUsageHolderKind",
]
