"""Synthetic Codex screens built only from markers the supported binary renders."""

#: Idle: the `›` composer above the model/directory footer.
CODEX_IDLE = "\n".join(
    [
        "> Ran ls",
        "",
        "\u203a Write tests for @filename",
        "gpt-5.6-sol medium \u00b7 /repo",
    ]
)

#: Approval overlay: a numbered option list ending in the `to cancel` footer.
CODEX_APPROVAL = "\n".join(
    [
        "  Would you like to run the following command?",
        "",
        "  rm -rf node_modules",
        "",
        "\u203a 1. Yes, proceed",
        "  2. No",
        "",
        "  Press enter to confirm or esc to cancel",
    ]
)
