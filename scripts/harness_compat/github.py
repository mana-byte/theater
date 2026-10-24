"""Upstream release discovery over public registries: stdlib only, and never partial."""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Iterable, Mapping
from urllib.parse import quote

from theater.harness.contracts.version_policy import version_key

_TIMEOUT_SECONDS = 30.0
_GITHUB_API = "https://api.github.com/"


def _get(url: str, headers: Mapping[str, str] | None = None) -> str:
    sent = {"User-Agent": "theater-harness-compat", **(headers or {})}
    # The runner's token only buys rate limit, and only for the API host.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith(_GITHUB_API):
        sent["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=sent), timeout=_TIMEOUT_SECONDS
    ) as response:
        return response.read().decode("utf-8")


def stable_ascending(versions: Iterable[str]) -> list[str]:
    """The canonical stable MAJOR.MINOR.PATCH releases among ``versions``, ascending, unique."""
    stable: set[str] = set()
    for version in versions:
        try:
            version_key(version)
        except ValueError:
            continue
        stable.add(version)
    return sorted(stable, key=version_key)


def npm_versions(package: str) -> list[str]:
    """Stable releases of an npm package (scoped names allowed), ascending."""
    url = f"https://registry.npmjs.org/{quote(package, safe='@')}"
    document = json.loads(_get(url, {"Accept": "application/vnd.npm.install-v1+json"}))
    return stable_ascending(document.get("versions", {}))


def pypi_versions(package: str) -> list[str]:
    """Every listed stable PyPI release, including yanked or unavailable distributions."""
    document = json.loads(_get(f"https://pypi.org/pypi/{quote(package)}/json"))
    return stable_ascending(document.get("releases", {}))


def github_release_versions(repo: str, *, tag_prefix: str = "v") -> list[str]:
    """Stable releases of ``owner/name`` whose tag starts with ``tag_prefix``, prefix stripped.

    Every page is read: a history cut short would silently skip old releases.
    """
    headers = {"Accept": "application/vnd.github+json"}
    versions: list[str] = []
    page = 1
    while True:
        url = f"{_GITHUB_API}repos/{repo}/releases?per_page=100&page={page}"
        releases = json.loads(_get(url, headers))
        for release in releases:
            tag = release.get("tag_name", "")
            if release.get("prerelease") or release.get("draft") or not tag.startswith(tag_prefix):
                continue
            versions.append(tag.removeprefix(tag_prefix))
        if len(releases) < 100:
            return stable_ascending(versions)
        page += 1
