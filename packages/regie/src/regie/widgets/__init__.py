"""Textual widgets owned by the independent Régie distribution."""

from regie.widgets.chrome import StatusLine
from regie.widgets.dashboard import CatalogDashboard
from regie.widgets.tree import ParticipantTree
from regie.widgets.usage_breakdown import UsageBreakdown
from regie.widgets.usage_footer import UsageFooter

__all__ = [
    "CatalogDashboard",
    "ParticipantTree",
    "StatusLine",
    "UsageBreakdown",
    "UsageFooter",
]
