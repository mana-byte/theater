"""OpenCode: stable releases and installation of ``@opencode/cli`` from npm."""

from .npm import NpmAdapter

ADAPTER = NpmAdapter("opencode", "@opencode/cli")
