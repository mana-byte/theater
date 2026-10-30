"""Claude hook payloads shared by the hook decode and identity tests."""

from __future__ import annotations

_SESSION = "11111111-1111-4111-8111-111111111111"
_TRANSCRIPT = (
    "/tmp/theater-claude-hook-proof/config/projects/-tmp-proof-cwd/"
    "11111111-1111-4111-8111-111111111111.jsonl"
)
_CWD = "/tmp/theater-claude-hook-proof/cwd"
_PROMPT_ID = "355bcbdd-d67d-47cd-9c5b-853c3609786a"
_TOOL_INPUT = {"command": "printf hook-proof", "description": "Print the string hook-proof"}


def captured_hooks() -> dict:
    """Synthetic payloads following the stock hook-input schemas."""
    return {
        "user_prompt_submit": {
            "session_id": _SESSION,
            "transcript_path": _TRANSCRIPT,
            "prompt_id": _PROMPT_ID,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "A human or queued followup prompt",
        },
        "stop": {
            "session_id": _SESSION,
            "transcript_path": _TRANSCRIPT,
            "prompt_id": _PROMPT_ID,
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": "An answer that must remain transcript-owned",
        },
        "pre_tool_use": {
            "session_id": _SESSION,
            "transcript_path": _TRANSCRIPT,
            "cwd": _CWD,
            "prompt_id": _PROMPT_ID,
            "permission_mode": "bypassPermissions",
            "effort": {"level": "high"},
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": dict(_TOOL_INPUT),
            "tool_use_id": "toolu_vrtx_0193M8yyTKVtZkqaCg3nJoif",
        },
        "post_tool_use": {
            "session_id": _SESSION,
            "transcript_path": _TRANSCRIPT,
            "cwd": _CWD,
            "prompt_id": _PROMPT_ID,
            "permission_mode": "bypassPermissions",
            "effort": {"level": "high"},
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": dict(_TOOL_INPUT),
            "tool_response": {
                "stdout": "hook-proof",
                "stderr": "",
                "interrupted": False,
                "isImage": False,
                "noOutputExpected": False,
            },
            "tool_use_id": "toolu_vrtx_0193M8yyTKVtZkqaCg3nJoif",
            "duration_ms": 411,
        },
        "post_tool_use_failure": {
            "session_id": "22222222-2222-4222-8222-222222222222",
            "transcript_path": (
                "/tmp/theater-claude-hook-failure-proof/config/projects/-tmp-proof-cwd/"
                "22222222-2222-4222-8222-222222222222.jsonl"
            ),
            "cwd": "/tmp/theater-claude-hook-failure-proof/cwd",
            "prompt_id": "1c55e1ba-bb09-4a10-9430-1f55317d6935",
            "permission_mode": "bypassPermissions",
            "effort": {"level": "high"},
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": "false", "description": "Run false command"},
            "tool_use_id": "toolu_vrtx_01FGkjuxkfjLk5ydGe5JsvZY",
            "error": "Exit code 1",
            "is_interrupt": False,
            "duration_ms": 231,
        },
    }
