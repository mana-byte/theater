"""Minimal standalone Régie launcher; bridge lifecycle ownership follows in Wave 12."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from regie.app import RegieApp
from regie.config import SettingsError, load_settings
from regie.constants import REGIE_REQUIRED_CAPABILITIES
from regie.contracts import PresentationOperations, PresentationTarget
from theater.frontend import FrontendClient


class _UnavailablePresentation(PresentationOperations):
    """Safe placeholder until the separately owned bridge supplies local presentation ops."""

    def can_stage(self, target: PresentationTarget) -> tuple[bool, str | None]:
        return False, "the local tmux bridge is not configured"

    async def terminal_exists(self, target: PresentationTarget) -> bool:
        return False

    async def stage_terminal(self, target: PresentationTarget, *, target_window: str) -> None:
        raise RuntimeError("the local tmux bridge is not configured")

    async def unstage_terminal(self, target: PresentationTarget) -> None:
        raise RuntimeError("the local tmux bridge is not configured")

    async def focus_terminal(self, target: PresentationTarget) -> None:
        raise RuntimeError("the local tmux bridge is not configured")

    async def resize_pane(
        self,
        pane_id: str,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="regie")
    home = Path(os.environ.get("THEATER_HOME", Path.home() / ".theater"))
    parser.add_argument("--socket", type=Path, default=home / "run" / "theater.sock")
    parser.add_argument("--config", type=Path, default=home / "regie" / "config.toml")
    parser.add_argument("--client-id", default="regie-ui")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except SettingsError as exc:
        parser.error(str(exc))
    client = FrontendClient(
        args.socket,
        client_id=args.client_id,
        required_capabilities=REGIE_REQUIRED_CAPABILITIES,
    )
    app = RegieApp(client=client, settings=settings, presentation=_UnavailablePresentation())
    app.run()
    return 0


__all__ = ["main"]
