"""Synthetic Claude screens built only from markers the supported binary renders."""

#: Idle footer hint; the composer line sits above it.
CLAUDE_IDLE = "? for shortcuts"

#: Complete ready footer with the native-agent count and context-clear hint.
CLAUDE_IDLE_TASK_FOOTER = (
    "⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent"
    "                        new task? /clear to save 122.3k tokens"
)

#: Permission dialog footer, shared by the write and bash dialog shapes.
CLAUDE_APPROVAL = "Esc to cancel"

#: Workspace-trust dialog option row.
CLAUDE_TRUST = "Yes, I trust this folder"
