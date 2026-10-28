"""Codex: stable GitHub releases (public tags as fallback), the release binary, its provisioning."""

from __future__ import annotations

import json
import os
import tarfile
import tempfile
import urllib.error
import zipfile
from pathlib import Path

from .. import github
from ..contract import InstallFailed, Sandbox
from ..process import run_group
from ..provisioning import CompatManifest, NativeEdit, ProvisionContext, Provisioned
from . import _codex_release

#: Used when the run names no logical models; two are needed to prove a selection takes effect.
_DEFAULT_MODELS = ("mock-a", "mock-b")
_PROVIDER = "mockprov"

#: Codex edits files through apply_patch; this patch adds one scratch file.
_ADD_FILE_PATCH = "*** Begin Patch\n*** Add File: {path}\n+{nonce}\n*** End Patch\n"


def _catalog_entry(slug: str) -> dict[str, object]:
    """The fewest fields the stock CLI parses; an uncatalogued model gets no apply_patch tool."""
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


_TAG_PREFIX = "rust-v"


def _public_git_tags() -> list[str]:
    """Stable releases from the public tags: the fallback when the GitHub API is rate limited."""
    with tempfile.TemporaryDirectory(prefix="codex-release-tags-") as home:
        env = {
            "HOME": home,
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": os.devnull,
        }
        done = run_group(
            [
                "git",
                "-c",
                "credential.helper=",
                "ls-remote",
                "--tags",
                f"https://github.com/{_codex_release.REPO}.git",
            ],
            cwd=Path(home),
            env=env,
            timeout=60,
        )
    if done.returncode:
        raise RuntimeError(f"Codex public tags unavailable: {done.stderr[-400:]}")
    prefix = f"refs/tags/{_TAG_PREFIX}"
    tags = (line.partition("\t")[2] for line in done.stdout.splitlines())
    versions = github.stable_ascending(
        tag.removeprefix(prefix) for tag in tags if tag.startswith(prefix)
    )
    if not versions:
        raise RuntimeError(f"Codex public tags response has no stable {_TAG_PREFIX} releases")
    return versions


class CodexAdapter:
    name = "codex"
    compat = COMPAT

    def discover(self) -> list[str]:
        try:
            return github.github_release_versions(_codex_release.REPO, tag_prefix=_TAG_PREFIX)
        except urllib.error.URLError:
            return _public_git_tags()

    def install(self, sb: Sandbox, version: str) -> Path:
        # The runner's token lets the digest check query the API; the CLI under test never sees it.
        tokens = {key: os.environ[key] for key in ("GH_TOKEN", "GITHUB_TOKEN") if key in os.environ}
        try:
            return _codex_release.fetch(version, sb.bin_dir, {**sb.env, **tokens})
        except (_codex_release.FetchError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
            raise InstallFailed(f"Codex {version} download failed: {error}") from error


ADAPTER = CodexAdapter()
