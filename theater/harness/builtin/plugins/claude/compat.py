"""How the compatibility suite points the stock Claude Code CLI at its mock model."""

from __future__ import annotations

import json

from theater.harness.contracts.compat import (
    CompatManifest,
    NativeEdit,
    ProvisionContext,
    Provisioned,
)

_DEFAULT_MODELS = ("mock-a", "mock-b")
_API_KEY = "compat-dummy-key"

#: Two harness-native efforts the CLI maps to different wire values.
_EFFORTS = ("low", "high")


def provision(context: ProvisionContext) -> Provisioned:
    """Point Messages requests at the mock and pre-answer fresh-home consent dialogs."""
    models = context.models or _DEFAULT_MODELS
    claude_home = context.home / ".claude"
    state = (
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "lastOnboardingVersion": context.version,
                "theme": "dark",
                "projects": {str(context.workdir.resolve()): {"hasTrustDialogAccepted": True}},
                "customApiKeyResponses": {"approved": [_API_KEY[-20:]], "rejected": []},
                "bypassPermissionsModeAccepted": True,
            }
        )
        + "\n"
    )
    return Provisioned(
        protocol="anthropic",
        env={
            "CLAUDE_CONFIG_DIR": str(claude_home),
            "ANTHROPIC_BASE_URL": context.mock_urls["anthropic"],
            "ANTHROPIC_API_KEY": _API_KEY,
            "ANTHROPIC_MODEL": models[0],
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
        },
        files={
            context.home / ".claude.json": state,
            claude_home / ".claude.json": state,
            claude_home / "settings.json": json.dumps(
                {"permissions": {"skipDangerousModePermissionPrompt": True}}
            )
            + "\n",
        },
        models=models,
        efforts=_EFFORTS,
    )


COMPAT = CompatManifest(
    provision=provision,
    native_edit=NativeEdit("Write", {"file_path": "{path}", "content": "{nonce}\n"}),
    canaries=("launch-files-dropped",),
)
