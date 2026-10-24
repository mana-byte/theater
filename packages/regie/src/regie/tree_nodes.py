"""Mutable node shapes shared by the tree projection and renderer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, NotRequired, TypedDict

from regie.marks import RowMark


class LayoutSeparator(TypedDict):
    name: str
    collapsed: NotRequired[Literal[True]]


class ParticipantNode(TypedDict):
    id: str
    parent_id: str | None
    tier: str
    harness: str
    icon: str | None
    status: str
    cwd: str | None
    name: str | None
    description: str | None
    addressable: bool
    human_presence: dict[str, str]
    trusted_identity: Mapping[str, object] | None
    transcript_identity: Mapping[str, object] | None
    tmux_pane: str | None
    usage_cost_microcents: int | None
    children: list[TreeNode]
    stage_reason: NotRequired[str]
    mark: NotRequired[RowMark]


class SeparatorNode(TypedDict):
    id: str
    kind: Literal["separator"]
    name: str
    collapsed: bool
    count: int
    children: list[TreeNode]
    folded_agents: NotRequired[list[TreeNode]]
    folded: NotRequired[list[str]]
    folded_status: NotRequired[str | None]
    stage_reason: NotRequired[str]


class PendingSpawnNode(TypedDict):
    id: str
    kind: Literal["spawn"]
    children: list[TreeNode]
    stage_reason: NotRequired[str]


type TreeNode = ParticipantNode | SeparatorNode | PendingSpawnNode


class RawRenderNode(TypedDict, total=False):
    """Optional renderer fields; callers may still pass any ordinary dictionary."""

    id: str
    kind: str
    name: str | None
    tier: str
    harness: str
    icon: str | None
    status: str
    cwd: str | None
    tmux_pane: str | None
    addressable: bool
    human_presence: dict[str, str]
    description: str | None
    stage_reason: str
    usage_cost_microcents: int | None
    mark: RowMark | None
    collapsed: bool
    count: int
    folded: list[str]
    folded_status: str | None
    children: list[RenderNode]


type RenderNode = TreeNode | RawRenderNode
