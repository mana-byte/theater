"""The régie: a Textual TUI that renders the agent tree and the bus.

The régie is itself a tmux pane — we adopt the user's session rather than
nesting. Left side: a lineage tree with live status. Bottom-left: scrolling
inter-agent traffic. Right side: a welcome dashboard (animated sentence and
usage tips) when no participant is staged; a real tmux pane — the stage —
replaces it once an agent is joined in, and the dashboard returns when it leaves.

The régie is read-mostly: navigate, select, zoom, kill, and one write action
— spawn. All prompting happens by focusing the stage and typing at the real
agent, because that is already a better interface than anything we would build.
"""
