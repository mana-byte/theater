"""The régie: a Textual TUI that renders the agent tree and the bus.

The régie is itself a tmux pane — we adopt the user's session rather than
nesting. Left side: a lineage tree with live status. Bottom-left: scrolling
inter-agent traffic. Right side: the stage, a real tmux pane showing the
selected agent fully interactive.

The régie is read-mostly: navigate, select, zoom, kill, and one write action
— spawn. All prompting happens by focusing the stage and typing at the real
agent, because that is already a better interface than anything we would build.
"""
