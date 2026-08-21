"""CLI error type for usage errors argparse cannot express."""

from __future__ import annotations


class BadUsage(Exception):
    """The command line is wrong in a way argparse cannot express."""
