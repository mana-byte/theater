"""Synthetic OpenCode screens built only from markers the supported binary renders."""

#: Idle composer footer (feature-plugins/prompt/footer.tsx, v2.2.0).
OPENCODE_IDLE = "shift+tab agents  ctrl+p commands"

#: Working composer footer hint (component/prompt/index.tsx).
OPENCODE_WORKING = "esc interrupt  ctrl+p commands"

#: After one Esc press the footer reads `esc again to interrupt` for ~5s.
OPENCODE_WORKING_INTERRUPT = "esc again to interrupt  ctrl+p commands"

#: Permission dialog header (routes/session/permission.tsx), no prompt chrome.
OPENCODE_APPROVAL = "Permission required"

#: Form dialog footer (mini/footer.form.tsx); classifies as an approval.
OPENCODE_QUESTION = "enter submit   esc dismiss"
