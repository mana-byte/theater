"""Coordinator-owned interfaces shared by the Régie UI and tmux bridge."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RegieSettings:
    """Standalone values loaded from Régie's own ``[regie]`` table."""

    theme: str | None = "ansi-dark"
    favourite: str | None = None
    tree_interval: float = 1.0
    bus_interval: float = 0.4
    bus_batch: int = 50
    cwd_segments: int = 2
    participant_detail: str = "cwd"
    sidebar_width: int = 52
    bus_visible: bool = False
    usage_visible: bool = False
    startup_reveal: bool = True
    cost_window: str = "day"
    dashboard_sentences: list[str] | None = None
    dashboard_sentence_hold_seconds: float = 10.0
    dashboard_sentence_char_interval: float = 0.1
    dashboard_tip_hold_seconds: float = 6.0
    dashboard_tip_char_interval: float = 0.04
    trajectory_page_size: int = 30


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Stable process inputs; provider identity and credentials live below ``state_dir``."""

    theater_socket: Path
    state_dir: Path
    selector: str = "tmux"
    client_id: str = "regie-tmux-bridge"
    reconnect_initial_seconds: float = 0.25
    reconnect_max_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class BridgeStatus:
    running: bool
    connection_state: str
    provider_id: str | None = None
    provider_generation: int | None = None
    process_id: int | None = None
    tmux_server_identity: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PresentationTarget:
    """Public terminal identity plus the provider kind used to gate local staging."""

    provider_id: str
    provider_kind: str
    terminal_id: str
    terminal_incarnation: str
    occupant: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LocalPresentationTarget:
    """A discovered local pane that may be staged but never controlled."""

    terminal_id: str


type StageTarget = PresentationTarget | LocalPresentationTarget


@dataclass(frozen=True, slots=True)
class UnmanagedPane:
    """Read-only local pane facts; this is never a daemon control identity."""

    pane_id: str
    command: str
    cwd: str | None = None
    session: str | None = None
    window_name: str | None = None
    harness: str | None = None

    def to_tree_row(self) -> dict[str, object]:
        return {
            "pane": self.pane_id,
            "command": self.command,
            "harness": self.harness or self.command,
            "cwd": self.cwd,
            "session": self.session,
            "window_name": self.window_name,
        }


class PresentationOperations(Protocol):
    """Identity-aware tmux presentation operations available to the TUI."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    def can_stage(self, target: StageTarget) -> tuple[bool, str | None]: ...

    async def target_window(self) -> str: ...

    async def terminal_exists(self, target: StageTarget) -> bool: ...

    async def stage_terminal(self, target: StageTarget, *, target_window: str) -> None: ...

    async def unstage_terminal(self, target: StageTarget) -> None: ...

    async def focus_terminal(self, target: StageTarget) -> None: ...

    async def resize_regie(self, *, width: int) -> None: ...

    async def resize_pane(
        self, pane_id: str, *, width: int | None = None, height: int | None = None
    ) -> None: ...

    async def copy_text(self, text: str) -> None: ...

    async def unmanaged_panes(
        self, *, harness_commands: Mapping[str, tuple[str, ...]]
    ) -> tuple[UnmanagedPane, ...]: ...


class BridgeRuntime(Protocol):
    """Persistent provider lifecycle implemented by ``regie.bridge.TmuxBridge``."""

    @property
    def status(self) -> BridgeStatus: ...

    async def run(self) -> None: ...

    async def close(self) -> None: ...


__all__ = [
    "BridgeConfig",
    "BridgeRuntime",
    "BridgeStatus",
    "LocalPresentationTarget",
    "PresentationOperations",
    "PresentationTarget",
    "RegieSettings",
    "StageTarget",
    "UnmanagedPane",
]
