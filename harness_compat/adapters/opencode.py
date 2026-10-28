"""OpenCode: stable releases and installation of ``@opencode/cli`` from npm."""

from ._npm import NpmAdapter

ADAPTER = NpmAdapter("opencode", "@opencode/cli")
