"""Claude Code: stable releases and installation from npm."""

from .npm import NpmAdapter

ADAPTER = NpmAdapter("claude", "@anthropic-ai/claude-code")
