from __future__ import annotations

import pytest
from regie.contracts import PresentationTarget, RegieSettings
from regie.controllers.session import SessionController
from regie.controllers.staging import StageController, StageOutcome

from theater.frontend import Participant, Provider


class Presentation:
    def __init__(self) -> None:
        self.staged: list[PresentationTarget] = []
        self.target_window_calls = 0
        self.resized: list[int] = []

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def can_stage(self, target: PresentationTarget) -> tuple[bool, str | None]:
        return True, None

    async def target_window(self) -> str:
        self.target_window_calls += 1
        return "@regie"

    async def terminal_exists(self, target: PresentationTarget) -> bool:
        return True

    async def stage_terminal(self, target: PresentationTarget, *, target_window: str) -> None:
        assert target_window == "@regie"
        self.staged.append(target)

    async def unstage_terminal(self, target: PresentationTarget) -> None:
        self.staged.remove(target)

    async def focus_terminal(self, target: PresentationTarget) -> None:
        assert target in self.staged

    async def resize_regie(self, *, width: int) -> None:
        self.resized.append(width)

    async def resize_pane(
        self,
        pane_id: str,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        return None


def _participant() -> Participant:
    return Participant.from_wire(
        {
            "participant_id": "participant-a",
            "origin": "spawned",
            "harness": "codex",
            "status": "idle",
            "owner": {"kind": "local_operator", "revision": 1},
            "addressable": True,
            "presence": "absent",
            "actions": {},
            "terminal_route": {
                "identity": {
                    "provider_id": "provider-a",
                    "provider_generation": 4,
                    "terminal_id": "%42",
                    "terminal_incarnation": "incarnation-a",
                    "occupant": {"harness": "codex"},
                    "process": None,
                },
                "health": "healthy",
            },
        }
    )


def _provider(kind: str) -> Provider:
    return Provider.from_wire(
        {
            "provider_id": "provider-a",
            "selector": "provider-a",
            "kind": kind,
            "generation": 4,
            "health": "healthy",
            "capabilities": [],
        }
    )


@pytest.mark.asyncio
async def test_other_provider_terminal_remains_visible_but_cannot_be_staged() -> None:
    presentation = Presentation()
    controller = StageController(RegieSettings(), presentation)

    result = await controller.stage(_participant(), {"provider-a": _provider("ssh")})

    assert result.outcome is StageOutcome.UNSTAGEABLE
    assert "cannot be staged" in (result.reason or "")
    assert presentation.staged == []


@pytest.mark.asyncio
async def test_stage_uses_only_the_public_terminal_identity_and_provider_kind() -> None:
    presentation = Presentation()
    controller = StageController(RegieSettings(), presentation)

    result = await controller.stage(_participant(), {"provider-a": _provider("tmux")})

    assert result.outcome is StageOutcome.STAGED
    assert presentation.staged == [
        PresentationTarget(
            provider_id="provider-a",
            provider_kind="tmux",
            terminal_id="%42",
            terminal_incarnation="incarnation-a",
            occupant={"harness": "codex"},
        )
    ]
    assert presentation.target_window_calls == 1
    assert presentation.resized == [52]


@pytest.mark.asyncio
async def test_stage_reports_an_identity_check_failure_without_mutating_terminals() -> None:
    class BrokenPresentation(Presentation):
        async def terminal_exists(self, target: PresentationTarget) -> bool:
            raise RuntimeError("bridge is reconnecting")

    presentation = BrokenPresentation()
    controller = StageController(RegieSettings(), presentation)

    result = await controller.stage(_participant(), {"provider-a": _provider("tmux")})

    assert result.outcome is StageOutcome.FAILED
    assert "verify terminal identity" in (result.reason or "")
    assert presentation.staged == []


@pytest.mark.asyncio
async def test_failed_replacement_does_not_leave_an_unstaged_terminal_selected() -> None:
    first = PresentationTarget("provider-a", "tmux", "%1", "first")
    replacement = PresentationTarget("provider-a", "tmux", "%2", "second")

    class FailingReplacementPresentation(Presentation):
        def __init__(self) -> None:
            super().__init__()
            self.unstaged: list[PresentationTarget] = []

        async def stage_terminal(self, target: PresentationTarget, *, target_window: str) -> None:
            if target == replacement:
                raise RuntimeError("pane disappeared")
            await super().stage_terminal(target, target_window=target_window)

        async def unstage_terminal(self, target: PresentationTarget) -> None:
            self.unstaged.append(target)
            await super().unstage_terminal(target)

    presentation = FailingReplacementPresentation()
    controller = SessionController(presentation)

    assert (await controller.stage(first)).staged is True
    failed = await controller.stage(replacement)
    await controller.close()

    assert failed.staged is False
    assert failed.target is None
    assert controller.target is None
    assert presentation.staged == []
    assert presentation.unstaged == [first]
