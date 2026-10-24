"""How the compatibility suite points the stock OpenCode CLI at its mock model."""

from __future__ import annotations

import json

from theater.harness.contracts.compat import (
    CompatManifest,
    NativeEdit,
    ProvisionContext,
    Provisioned,
)

#: Used when the run names no logical models; two are needed to prove a selection takes effect.
_DEFAULT_MODELS = ("mock-a", "mock-b")
_PROVIDER = "mockprov"


def provision(context: ProvisionContext) -> Provisioned:
    """A global config with one OpenAI-compatible provider serving each logical model.

    The launch plan owns ``OPENCODE_CONFIG`` and the model, so the provider rides the global
    config that every launch merges beneath it; selectors are ``<provider>/<model>``.
    """
    models = context.models or _DEFAULT_MODELS
    config = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        # Launches without a model selection would otherwise fall back to a hosted default.
        "model": f"{_PROVIDER}/{models[0]}",
        # The title agent would otherwise spend the first scripted reply before the real turn.
        "agent": {"title": {"disable": True}},
        "provider": {
            _PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Mock",
                "options": {"baseURL": context.mock_urls["openai"], "apiKey": "mock-key"},
                "models": {name: {"name": name} for name in models},
            }
        },
    }
    return Provisioned(
        protocol="openai",
        env={
            # No update check or catalog refresh may reach for the dead proxy during startup.
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
        },
        files={context.config_dir / "opencode" / "opencode.json": json.dumps(config, indent=2)},
        models=tuple(f"{_PROVIDER}/{name}" for name in models),
    )


COMPAT = CompatManifest(
    provision=provision,
    # 2.x names the target `path`, 1.x `filePath`; each line ignores the key it does not know.
    native_edit=NativeEdit(
        "write", {"path": "{path}", "filePath": "{path}", "content": "{nonce}\n"}
    ),
)
