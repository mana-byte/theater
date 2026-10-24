"""How the compatibility suite points the stock Pi CLI at its mock model."""

from __future__ import annotations

import json

from theater.harness.contracts.compat import CompatManifest, ProvisionContext, Provisioned

#: Used when the run names no logical models; two are needed to prove a selection takes effect.
_DEFAULT_MODELS = ("mock-a", "mock-b")
_PROVIDER = "mockprov"


def provision(context: ProvisionContext) -> Provisioned:
    """A custom provider serving each logical model from the mock over OpenAI completions."""
    models = context.models or _DEFAULT_MODELS
    agent_dir = context.home / ".pi" / "agent"
    # Custom provider: baseUrl is the mock, a literal apiKey makes the models selectable.
    catalog = {
        "providers": {
            _PROVIDER: {
                "baseUrl": context.mock_urls["openai-chat"],
                "api": "openai-completions",
                "apiKey": "mock-key",
                "models": [{"id": name} for name in models],
            }
        }
    }
    # Start on the mock provider, trust the workdir so the TUI never prompts, stay offline.
    settings = {
        "defaultProvider": _PROVIDER,
        "defaultModel": models[0],
        "defaultProjectTrust": "always",
        "quietStartup": True,
        "enableInstallTelemetry": False,
        "enableAnalytics": False,
    }
    # Pi canonicalises cwd with realpath before the trust lookup.
    trust = {str(context.workdir.resolve()): True}
    return Provisioned(
        protocol="openai-chat",
        env={"PI_CODING_AGENT_DIR": str(agent_dir)},
        files={
            agent_dir / "models.json": json.dumps(catalog, indent=2) + "\n",
            agent_dir / "settings.json": json.dumps(settings, indent=2) + "\n",
            agent_dir / "trust.json": json.dumps(trust, indent=2) + "\n",
        },
        models=models,
    )


COMPAT = CompatManifest(provision=provision)
