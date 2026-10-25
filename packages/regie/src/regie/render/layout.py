"""Layout: key types, path shortening, forest walk, and the flat row list.

Leaves are three rows (spec §v1.9): incoming rail, status/name, cwd. Content, not
Rich Text, so ``$primary`` and friends resolve against the active Textual theme.
"""

from __future__ import annotations

# ruff: noqa: I001
from collections.abc import Iterable, Sequence
from typing import cast

from textual.content import Content

from regie.ui_constants import (
    REGIE_TREE_BRANCH as BRANCH,
    REGIE_TREE_GAP as GAP,
    REGIE_TREE_LAST_BRANCH as LAST_BRANCH,
    REGIE_TREE_LEAF_ROWS as LEAF_ROWS,
    REGIE_TREE_RAIL as RAIL,
    REGIE_TREE_SEPARATOR_ROWS as SEPARATOR_ROWS,
)
from regie.render.glyphs import node_label, separator_label
from regie.render.menu import menu_label
from regie.tree_nodes import RawRenderNode, RenderNode, TreeNode

#: A stable row identity for widget reconciliation; the first element namespaces the row kind.
type Key = tuple[str, str]
type RenderedLine = tuple[Content, RenderNode, Key, str, str]


def row_count(key: Key, node: RenderNode | None = None) -> int:
    """Rendered rows for one tree line: a separator is shorter than a leaf, the folded menu and
    its info rows one line."""
    if key[0] == "g":
        return LEAF_ROWS if node is not None and node.get("open") else 1
    return {"s": SEPARATOR_ROWS, "i": 1}.get(key[0], LEAF_ROWS)


class TreeLines(list[RenderedLine]):
    """Rendered lines with a constant-time rendered-row coordinate lookup."""

    __slots__ = ("row_lookup",)

    def __init__(self, lines: Iterable[RenderedLine]) -> None:
        super().__init__(lines)
        self.row_lookup = tuple(
            (line_index, row)
            for line_index, (_, node, key, _, _) in enumerate(self)
            for row in range(row_count(key, node))
        )


def shorten_path(path: str | None, keep: int = 2) -> str:
    """Keep the last *keep* segments, elide the rest with ``…/``.

    Runs after :func:`regie.formatting.tilde`, so ``~`` is preserved and not counted
    as a segment (``~/a/b/c -> ~/…/b/c``). Empty returns ``"-"`` like ``tilde()``.
    """
    if not path:
        return "-"

    # Separate a leading ``~`` (or ``~/``) prefix so the home mark is carried without being counted.
    prefix = ""
    rest = path
    if rest.startswith("~/"):
        prefix = "~/"
        rest = rest[2:]
    elif rest == "~":
        return "~"

    segments = [s for s in rest.split("/") if s]

    if len(segments) <= keep:
        return path

    tail = "/".join(segments[-keep:])
    return f"{prefix}…/{tail}"


def is_root_prefix(prefix: str) -> bool:
    """Whether *prefix* is a bare branch, i.e. a root branching off the super-root."""
    return prefix in (BRANCH, LAST_BRANCH)


def _walk(
    nodes: Sequence[RenderNode], prefix: str = "", depth: int = 0, *, is_first_root: bool = False
) -> list[tuple[str, RenderNode, Key, str, bool]]:
    """Depth-first walk that pairs each node with its drawn ancestry.

    Roots branch off an invisible super-root so rails connect the forest; only the
    first root blanks row 1, since a rail off the panel top reads as a missing row.
    """
    rows: list[tuple[str, RenderNode, Key, str, bool]] = []
    last_index = len(nodes) - 1
    for i, node in enumerate(nodes):
        last = i == last_index
        if depth == 0:
            # Roots branch off the invisible super-root.
            branch = LAST_BRANCH if last else BRANCH
            child_prefix = GAP if last else RAIL
            first_root = is_first_root and i == 0
        else:
            branch = LAST_BRANCH if last else BRANCH
            child_prefix = prefix + (GAP if last else RAIL)
            first_root = False
        # cont_prefix for row 3 is the rail/gap children inherit — already child_prefix.
        cont_prefix = child_prefix
        kind = {"separator": "s", "spawn": "n", "menu_root": "g", "menu": "m", "info": "i"}.get(
            str(node.get("kind")), "p"
        )
        key: Key = (kind, node.get("id", ""))
        rows.append((prefix + branch, node, key, cont_prefix, first_root))
        rows += _walk(node.get("children") or [], child_prefix, depth + 1)
    return rows


def _labelled(
    row: tuple[str, RenderNode, Key, str, bool], *, cwd_segments: int = 2, frame: int = 0
) -> RenderedLine:
    prefix, node, key, cont_prefix, is_first_root = row
    if key[0] in "gmi":
        label = menu_label(
            cast(RawRenderNode, node), prefix, cont_prefix, is_first_root=is_first_root
        )
        return label, node, key, prefix, cont_prefix
    if key[0] == "s":
        view = cast(RawRenderNode, node)
        label = separator_label(
            str(view.get("name", "")),
            prefix,
            count=int(view.get("count") or 0),
            collapsed=bool(view.get("collapsed")),
            status=view.get("folded_status"),
            is_first_root=is_first_root,
        )
        return label, node, key, prefix, cont_prefix
    return (
        node_label(
            node,
            prefix,
            cont_prefix=cont_prefix,
            cwd_segments=cwd_segments,
            frame=frame,
            is_first_root=is_first_root,
        ),
        node,
        key,
        prefix,
        cont_prefix,
    )


def render_tree(
    tree: list[TreeNode] | list[dict],
    unmanaged: list[dict] | None = None,
    *,
    cwd_segments: int = 2,
    menu: RawRenderNode | None = None,
) -> TreeLines:
    """Produce (label, data, key, prefix, cont_prefix) 5-tuples for the Tree widget.

    Keys are stable reconcile ids; prefixes are carried so spinner ticks re-render
    leaves without re-walking the tree.
    """
    # Raw renderer callers retain their dictionaries and missing-field fallbacks.
    nodes = cast(Sequence[RenderNode], tree)
    if menu is not None:  # the options menu is one more root, last
        nodes = [*nodes, menu]
    lines = [_labelled(row, cwd_segments=cwd_segments) for row in _walk(nodes, is_first_root=True)]
    if unmanaged:
        lines.append(
            (
                Content.assemble(("── unmanaged ──", "$text dim italic")),
                {},
                ("sep", "unmanaged"),
                "",
                "",
            )
        )
        for u in unmanaged:
            fake_node: RawRenderNode = {
                "id": u.get("pane", "????????"),
                "tier": "external",
                "harness": u.get("harness", u.get("command", "?")),
                "icon": u.get("icon"),
                "status": "idle",
                "cwd": u.get("cwd"),
                "tmux_pane": u.get("pane"),
                "addressable": False,
                "children": [],
            }
            key: Key = ("u", u.get("pane", ""))
            lines.append((node_label(fake_node, cwd_segments=cwd_segments), fake_node, key, "", ""))
    return TreeLines(lines)


def selected_participant(lines: list[RenderedLine], index: int) -> RenderNode | None:
    """The participant dict at a given line index, or None if it's a separator."""
    if 0 <= index < len(lines):
        _, node, key, _, _ = lines[index]
        if key[0] == "p" and node.get("id"):
            return node
    return None
