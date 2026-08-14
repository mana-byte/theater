"""Theater — cross-harness orchestration layer for coding agents."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    # Single source of truth: the version declared in pyproject.toml, read
    # from the installed package metadata so this never drifts from the build.
    __version__ = _version("theater")
except PackageNotFoundError:  # running from a source tree with nothing installed
    __version__ = "0.0.0+unknown"
