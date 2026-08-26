"""Daemon-side agent telemetry projection."""

from .catalog import AGENT_METRIC_SPECS
from .projector import AgentTelemetry, create_agent_telemetry

__all__ = ["AGENT_METRIC_SPECS", "AgentTelemetry", "create_agent_telemetry"]
