"""Pi: the exact stock package installed from npm, provisioned against the mock model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..contract import InstallFailed, Sandbox
from ..mock.provisioning import CompatManifest, ProvisionContext, Provisioned
from . import _npm, _registries

#: Used when the run names no logical models; two are needed to prove a selection takes effect.
_DEFAULT_MODELS = ("mock-a", "mock-b")
_PROVIDER = "mockprov"

#: Two harness-native efforts the CLI maps to different wire values.
_EFFORTS = ("low", "high")


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
                # Pi sends a reasoning effort only for a model declared as reasoning-capable.
                "models": [{"id": name, "reasoning": True} for name in models],
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
        efforts=_EFFORTS,
    )


COMPAT = CompatManifest(provision=provision)


_PACKAGE = "@earendil-works/pi-coding-agent"


class PiAdapter:
    name = "pi"
    compat = COMPAT

    def discover(self) -> list[str]:
        return _registries.npm_versions(_PACKAGE)

    def install(self, sb: Sandbox, version: str) -> Path:
        spec = f"{_PACKAGE}@{version}"
        _npm.install(sb, sb.bin_dir, [spec])
        package_root = sb.bin_dir / "node_modules" / "@earendil-works" / "pi-coding-agent"
        try:
            document: dict[str, Any] = json.loads(
                (package_root / "package.json").read_text(encoding="utf-8")
            )
            declared = document["bin"]
            relative = declared["pi"] if isinstance(declared, dict) else declared
            target = (package_root / relative).resolve(strict=True)
            target.relative_to(package_root.resolve())
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InstallFailed(
                f"installed Pi {version} has no valid pi package bin: {exc}"
            ) from exc
        return _npm.expose(sb, "pi", target)


ADAPTER = PiAdapter()
