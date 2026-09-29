"""Launch-argument safety for prompt text.

Commander (Claude), clap (Codex), yargs (OpenCode 1.x), and effect's CLI
(OpenCode 2.x) all route by the first character of an argv element, so a prompt
starting with "-" would be parsed as an option instead of the prompt.
"""

from __future__ import annotations

#: A blank first line: harmless to the model, invisible to every parser.
PROMPT_OPTION_GUARD = "\n"


def literal_prompt_argument(prompt: str) -> str:
    """Return ``prompt`` shaped so a CLI keeps it one literal argv element."""
    if prompt.startswith("-"):
        return f"{PROMPT_OPTION_GUARD}{prompt}"
    return prompt
