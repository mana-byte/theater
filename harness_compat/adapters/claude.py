"""Claude Code: stable releases and installation from npm."""

from ._npm import NpmAdapter

ADAPTER = NpmAdapter("claude", "@anthropic-ai/claude-code")
