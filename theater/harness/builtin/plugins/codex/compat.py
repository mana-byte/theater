"""How the compatibility suite points the stock Codex CLI at its mock model."""

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

#: Codex edits files through apply_patch; this patch adds one scratch file.
_ADD_FILE_PATCH = "*** Begin Patch\n*** Add File: {path}\n+{nonce}\n*** End Patch\n"


def _catalog_entry(slug: str) -> dict[str, object]:
    """The fewest fields Codex 0.163 parses; an uncatalogued model gets no apply_patch tool."""
    return {
        "slug": slug,
        "display_name": slug,
        "description": "Theater compatibility mock",
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [{"effort": "medium", "description": "medium"}],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 1,
        "support_verbosity": True,
        "apply_patch_tool_type": "freeform",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "supports_parallel_tool_calls": False,
        "context_window": 272000,
        "base_instructions": "You are a test agent.",
        "experimental_supported_tools": [],
    }


#: Two harness-native efforts the CLI maps to different wire values.
_EFFORTS = ("low", "high")


def provision(context: ProvisionContext) -> Provisioned:
    """A CODEX_HOME whose config serves each logical model over Responses from a trusted workdir."""
    models = context.models or _DEFAULT_MODELS
    codex_home = context.home / ".codex"
    catalog = codex_home / "catalog.json"
    # Codex resolves the cwd before comparing project trust, so trust the resolved path.
    workdir = context.workdir.resolve()
    config = (
        f'model = "{models[0]}"\n'
        f'model_provider = "{_PROVIDER}"\n'
        f"model_catalog_json = {json.dumps(str(catalog))}\n"
        # The stock TUI renders a blocking update dialog before the composer otherwise.
        "check_for_update_on_startup = false\n\n"
        f"[model_providers.{_PROVIDER}]\n"
        'name = "Theater compatibility mock"\n'
        f'base_url = "{context.mock_urls["openai-responses"]}"\n'
        'wire_api = "responses"\n\n'
        f"[projects.{json.dumps(str(workdir))}]\n"
        'trust_level = "trusted"\n'
    )
    return Provisioned(
        protocol="openai-responses",
        # The launch planner pins CODEX_HOME to this value, so the child reads this config only.
        env={"CODEX_HOME": str(codex_home)},
        files={
            codex_home / "config.toml": config,
            catalog: json.dumps({"models": [_catalog_entry(name) for name in models]}),
        },
        models=models,
        efforts=_EFFORTS,
    )


COMPAT = CompatManifest(
    provision=provision,
    native_edit=NativeEdit("apply_patch", _ADD_FILE_PATCH),
    acknowledged_routes=(("POST", "/v1/analytics/codex/turn-costs"),),
)
