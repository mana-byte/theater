"""Upstream release discovery over public registries: unauthenticated, stdlib only."""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Iterable, Mapping
from urllib.parse import quote

from .contract import is_stable, version_key

_TIMEOUT_SECONDS = 30.0
_GITHUB_PAGES = 5


def _get(url: str, headers: Mapping[str, str] | None = None) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "theater-harness-compat", **(headers or {})}
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8")


def _stable_ascending(versions: Iterable[str]) -> list[str]:
    return sorted({version for version in versions if is_stable(version)}, key=version_key)


def npm_versions(package: str) -> list[str]:
    """Stable releases of an npm package (scoped names allowed), ascending."""
    url = f"https://registry.npmjs.org/{quote(package, safe='@')}"
    document = json.loads(_get(url, {"Accept": "application/vnd.npm.install-v1+json"}))
    return _stable_ascending(document.get("versions", {}))


def pypi_versions(package: str) -> list[str]:
    """Stable, un-yanked releases of a PyPI project, ascending."""
    document = json.loads(_get(f"https://pypi.org/pypi/{quote(package)}/json"))
    live = (
        version
        for version, files in document.get("releases", {}).items()
        if files and not all(item.get("yanked") for item in files)
    )
    return _stable_ascending(live)


def github_release_versions(repo: str, *, tag_prefix: str = "v") -> list[str]:
    """Stable releases of ``owner/name`` whose tag starts with ``tag_prefix``, prefix stripped."""
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    versions: list[str] = []
    for page in range(1, _GITHUB_PAGES + 1):
        url = f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
        releases = json.loads(_get(url, headers))
        for release in releases:
            tag = release.get("tag_name", "")
            if release.get("prerelease") or release.get("draft") or not tag.startswith(tag_prefix):
                continue
            versions.append(tag.removeprefix(tag_prefix))
        if len(releases) < 100:
            break
    return _stable_ascending(versions)


def raw_file(repo: str, ref: str, path: str) -> str:
    """One file of ``owner/name`` at a tag, branch or commit, from raw.githubusercontent.com."""
    return _get(f"https://raw.githubusercontent.com/{repo}/{quote(ref)}/{path}")
