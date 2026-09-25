from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
from typing import ClassVar

from regie.app import RegieApp
from regie.app_parts.organization import TreeOrganization
from regie.tree_layout import TreeLayout
from regie.widgets import ParticipantTree
from textual.app import App, ComposeResult
from textual.binding import BindingType

from theater.frontend import EventCursor, Participant, StateProjection


def _participant(
    participant_id: str,
    *,
    parent_id: str | None = None,
    created_at: float = 1.0,
) -> Participant:
    return Participant.from_wire(
        {
            "participant_id": participant_id,
            "origin": "spawned",
            "harness": "codex",
            "status": "idle",
            "owner": {"kind": "local_operator", "revision": 1},
            "parent_id": parent_id,
            "addressable": True,
            "presence": "absent",
            "actions": {},
            "created_at": created_at,
        }
    )


def _projection(*participants: Participant) -> StateProjection:
    return StateProjection(
        cursor=EventCursor("stream-a", 1),
        participants=MappingProxyType({item.participant_id: item for item in participants}),
        operations=MappingProxyType({}),
        jobs=MappingProxyType({}),
        providers=MappingProxyType({}),
        workspaces=MappingProxyType({}),
    )


class _OrganizationApp(TreeOrganization, App[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        binding for binding in RegieApp.BINDINGS if binding.action.startswith("move_tree_row")
    ]

    def __init__(self, projection: StateProjection) -> None:
        super().__init__()
        self._state = SimpleNamespace(projection=projection)
        self._tree_layout = TreeLayout()

    def compose(self) -> ComposeResult:
        yield ParticipantTree(startup_reveal=False)

    def on_mount(self) -> None:
        self._show_projection(self._state.projection)

    def _show_projection(self, projection: StateProjection) -> None:
        self.query_one(ParticipantTree).show_projection(
            projection,
            layout=self._tree_layout.to_mapping(),
        )


async def test_jk_reorders_roots_and_keeps_a_child_with_its_parent() -> None:
    root_a = _participant("root-a", created_at=1)
    child_a = _participant("child-a", parent_id="root-a", created_at=2)
    child_b = _participant("child-b", parent_id="root-a", created_at=3)
    root_b = _participant("root-b", created_at=4)
    app = _OrganizationApp(_projection(root_a, child_a, child_b, root_b))

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await pilot.press("J")
        assert tree.participant_ids == ("root-b", "root-a", "child-a", "child-b")
        assert tree.selected_key == ("p", "root-a")

        tree.select("child-a")
        await pilot.press("J")
        assert tree.participant_ids == ("root-b", "root-a", "child-b", "child-a")
        assert tree.selected_key == ("p", "child-a")
        assert app._tree_layout.orders[""] == ["root-b", "root-a"]
        assert app._tree_layout.orders["root-a"] == ["child-b", "child-a"]
