"""How the compatibility suite points the stock Vibe CLI at its mock model."""

from __future__ import annotations

from theater.harness.contracts.compat import (
    CompatManifest,
    McpCall,
    NativeEdit,
    ProvisionContext,
    Provisioned,
)

#: Used when the run names no logical models; two are needed to prove a selection takes effect.
_DEFAULT_MODELS = ("mock-a", "mock-b")
_PROVIDER = "mockprov"


def provision(context: ProvisionContext) -> Provisioned:
    """A config with one OpenAI-style provider serving each logical model, and a trusted workdir."""
    models = context.models or _DEFAULT_MODELS
    vibe_home = context.home / ".vibe"
    config = (
        f'active_model = "{models[0]}"\n'
        f'[[providers]]\nname = "{_PROVIDER}"\n'
        f'api_base = "{context.mock_urls["openai-chat"]}"\n'
        'api_key_env_var = ""\napi_style = "openai"\nbackend = "generic"\n'
    ) + "".join(
        f'[[models]]\nname = "{name}"\nprovider = "{_PROVIDER}"\nalias = "{name}"\n'
        for name in models
    )
    # Vibe resolves the workdir before comparing, so trust the resolved path.
    trusted = f'trusted = ["{context.workdir.resolve()}"]\nuntrusted = []\n'
    return Provisioned(
        protocol="openai-chat",
        env={"VIBE_HOME": str(vibe_home)},
        files={
            vibe_home / "config.toml": config,
            vibe_home / "trusted_folders.toml": trusted,
        },
        models=models,
    )


COMPAT = CompatManifest(
    provision=provision,
    native_edit=NativeEdit("write_file", {"path": "{path}", "content": "{nonce}\n"}),
    # Vibe offers MCP tools only through its script runner, as `tools.mcp_<server>.<tool>`.
    mcp_call=McpCall(
        "run_typescript", {"code": "return await tools.mcp_{server}.{tool}({arguments});"}
    ),
    canaries=("launch-env-dropped",),
)
