from __future__ import annotations

import subprocess
import sys
from typing import cast

from regie.app import RegieApp
from regie.contracts import PresentationTarget, RegieSettings

from theater.frontend import FrontendClient


class Client:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"RegieApp construction unexpectedly accessed client.{name}")


class Presentation:
    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def can_stage(self, target: PresentationTarget) -> tuple[bool, str | None]:
        return False, "fixture"

    async def target_window(self) -> str:
        raise AssertionError("construction must not discover a target window")

    async def terminal_exists(self, target: PresentationTarget) -> bool:
        return False

    async def stage_terminal(self, target: PresentationTarget, *, target_window: str) -> None:
        raise AssertionError("construction must not stage a terminal")

    async def unstage_terminal(self, target: PresentationTarget) -> None:
        raise AssertionError("construction must not unstage a terminal")

    async def focus_terminal(self, target: PresentationTarget) -> None:
        raise AssertionError("construction must not focus a terminal")

    async def resize_regie(self, *, width: int) -> None:
        raise AssertionError("construction must not resize the Régie pane")

    async def resize_pane(
        self,
        pane_id: str,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        raise AssertionError("construction must not resize a pane")


def test_regie_app_requires_explicit_public_client_settings_and_presentation() -> None:
    app = RegieApp(
        client=cast(FrontendClient, Client()),
        settings=RegieSettings(),
        presentation=Presentation(),
    )

    assert app.projection is None
    assert app.settings.sidebar_width == 52


def test_regie_keeps_the_hidden_high_priority_tmux_return_signal() -> None:
    binding = next(item for item in RegieApp.BINDINGS if item.key == "ctrl+g")

    assert binding.action == "return_to_tree"
    assert binding.show is False
    assert binding.priority is True


def test_startup_imports_defer_bridge_worker_and_optional_trajectory_widgets():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import regie.cli, regie.app, sys; "
            "assert 'regie.bridge.runtime' not in sys.modules; "
            "assert 'regie.trajectory.rich.view' not in sys.modules; "
            "from regie.trajectory import TrajectoryView; "
            "from regie.trajectory.rich.view import TrajectoryView as View; "
            "assert TrajectoryView is View",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
