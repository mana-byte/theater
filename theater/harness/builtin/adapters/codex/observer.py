from __future__ import annotations

from pathlib import Path

from theater.harness.observation import TranscriptObserver
from theater.provenance import TranscriptProvenance, normalize_provenance
from theater.trajectory.capabilities import TrajectoryCapabilities, TrajectoryFeature

from .identity import CodexIdentityMixin
from .parser import CodexParserMixin
from .screen import CodexScreenMixin
from .trajectory import CodexTrajectoryMixin


class CodexObserver(
    CodexIdentityMixin,
    CodexParserMixin,
    CodexTrajectoryMixin,
    CodexScreenMixin,
    TranscriptObserver,
):
    """Read `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.

    Note the date directories: rollout files are filed under the day the
    session started, in UTC, which is not the local date for most of the world
    for part of every day.
    """

    #: The process holds its rollout open, so ownership can be shown rather than inferred.
    proves_ownership = True
    trajectory_capabilities = TrajectoryCapabilities(
        supported=frozenset(
            {
                TrajectoryFeature.REQUESTS,
                TrajectoryFeature.MODELS,
                TrajectoryFeature.TOOLS,
                TrajectoryFeature.USAGE,
                TrajectoryFeature.TIMING,
                TrajectoryFeature.REASONING,
                TrajectoryFeature.CONTEXT,
                TrajectoryFeature.LIVE_UPDATES,
            }
        ),
        unsupported=frozenset({TrajectoryFeature.RETRIES}),
    )

    def __init__(
        self,
        root: Path | None = None,
        pane_pid: int | None = None,
        session_exact: bool = False,
        session_provenance: str | TranscriptProvenance | None = None,
    ):
        #: Injectable so tests never touch the real ~/.codex.
        self.root = root or Path.home() / ".codex" / "sessions"
        #: The participant's launch process. Set only on the `open_source_for` clone.
        self.pane_pid = pane_pid
        self._last_model: str | None = None
        self._last_provider: str | None = None
        self._last_cwd: str | None = None
        self._active_turn_id: str | None = None
        self._pending_patch_exec: tuple[str, float] | None = None
        self._mcp_calls: dict[str, tuple[str, str]] = {}
        #: Whether the id this clone opened with is itself proof — token or receipt, not file-read.
        provenance = normalize_provenance(session_provenance)
        self._session_exact = session_exact or provenance is TranscriptProvenance.EXACT
        #: Rollouts held open by this clone's process; resolved so another spelling still matches.
        self._proved: set[Path] = set()
