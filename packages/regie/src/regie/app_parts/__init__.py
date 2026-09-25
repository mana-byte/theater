"""Cohesive mixins that together make up ``RegieApp``; none imports ``regie.app``."""

from regie.app_parts.actions import ActionTracking
from regie.app_parts.controls import ControlActions
from regie.app_parts.diagnostics import DiagnosticsDisplay
from regie.app_parts.navigation import TreeNavigation
from regie.app_parts.organization import TreeOrganization
from regie.app_parts.projection import ProjectionSync
from regie.app_parts.renaming import RenameActions
from regie.app_parts.spawning import SpawnResume
from regie.app_parts.staging import StagingActions
from regie.app_parts.startup import StartupLoading
from regie.app_parts.trajectory import TrajectoryActions
from regie.app_parts.transcripts import TranscriptRecovery
from regie.app_parts.usage import UsageFooter

__all__ = [
    "ActionTracking",
    "ControlActions",
    "DiagnosticsDisplay",
    "ProjectionSync",
    "RenameActions",
    "SpawnResume",
    "StagingActions",
    "StartupLoading",
    "TrajectoryActions",
    "TranscriptRecovery",
    "TreeNavigation",
    "TreeOrganization",
    "UsageFooter",
]
