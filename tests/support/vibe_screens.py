"""Synthetic Vibe screens built only from markers the supported binary renders."""

#: Idle composer marker (input_modes.rs: `Prompt => "> "`).
VIBE_IDLE = ">"

#: The spinner hint stays under an open dialog; approval must win over it.
VIBE_APPROVAL = "\n".join(
    [
        "⠋ Waiting for approval… (12s Esc/Ctrl+C to interrupt)",
        "  ↑↓/jk navigate  Enter select  Esc reject",
    ]
)

#: Working spinner hint (ui/loading/hint.rs); `Esc to interrupt` is the queued spelling.
VIBE_WORKING = "\n".join(
    [
        "⣄  Generating… (1m23s Esc/Ctrl+C to interrupt)",
        ">",
    ]
)
VIBE_WORKING_QUEUED = "\n".join(
    [
        "⣄  Generating… (1m23s Esc to interrupt)",
        ">",
    ]
)

#: Workspace-trust warning (ui/trust_folders_layout.rs).
VIBE_TRUST = "Malicious configs can modify AI behavior, exfiltrate data, run destructive commands."

#: Question dialog: the action-required loading row plus the question footer.
VIBE_QUESTION = "\n".join(
    [
        "⠐⠋ User input required… (9s Esc/Ctrl+C to interrupt)",
        "  Which answer do you prefer?",
        "    1. Concise and direct",
        "    2. Short and casual",
        "  ↑↓/jk navigate  Enter select  Esc cancel",
    ]
)
