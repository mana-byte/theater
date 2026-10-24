"""Codex: stable releases from GitHub (public git tags as fallback) and the release binary."""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
import urllib.error
import zipfile
from pathlib import Path

from .. import github
from ..contract import InstallFailed, Sandbox
from . import codex_release

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
        done = subprocess.run(
            [
                "git",
                "-c",
                "credential.helper=",
                "ls-remote",
                "--tags",
                f"https://github.com/{codex_release.REPO}.git",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
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

    def discover(self) -> list[str]:
        try:
            return github.github_release_versions(codex_release.REPO, tag_prefix=_TAG_PREFIX)
        except urllib.error.URLError:
            return _public_git_tags()

    def install(self, sb: Sandbox, version: str) -> Path:
        # The runner's token lets the digest check query the API; the CLI under test never sees it.
        tokens = {key: os.environ[key] for key in ("GH_TOKEN", "GITHUB_TOKEN") if key in os.environ}
        try:
            return codex_release.fetch(version, sb.bin_dir, {**sb.env, **tokens})
        except (codex_release.FetchError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
            raise InstallFailed(f"Codex {version} download failed: {error}") from error


ADAPTER = CodexAdapter()
