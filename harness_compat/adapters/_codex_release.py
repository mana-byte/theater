"""Fetch an exact codex-cli release: downloaded, digest-checked and unpacked, never installed."""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import tarfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

from ..process import run_group

REPO = "openai/codex"
_DOWNLOAD_TIMEOUT_SECONDS = 600.0
_API_TIMEOUT_SECONDS = 60.0

#: Release asset per (os, arch); fail closed on anything unmapped.
ASSET_BY_PLATFORM: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "codex-aarch64-apple-darwin.tar.gz",
    ("darwin", "x86_64"): "codex-x86_64-apple-darwin.tar.gz",
    ("linux", "x86_64"): "codex-x86_64-unknown-linux-musl.tar.gz",
    ("linux", "aarch64"): "codex-aarch64-unknown-linux-musl.tar.gz",
}


class FetchError(RuntimeError):
    """The fetch refused to proceed; the message says why."""


def release_tag(version: str) -> str:
    return f"rust-v{version}"


def asset_name() -> str:
    key = (platform.system().lower(), platform.machine())
    if (asset := ASSET_BY_PLATFORM.get(key)) is None:
        raise FetchError(f"no codex release asset is mapped for {key}")
    return asset


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    command: list[str], *, cwd: Path, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    try:
        return run_group(command, cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise FetchError(f"{' '.join(command)} did not finish within {timeout}s") from error
    except OSError as error:
        raise FetchError(f"could not run {command[0]}: {error}") from error


def _download(tag: str, asset: str, dest: Path, env: Mapping[str, str]) -> Path:
    """Fetch the release URL with curl (no API quota), falling back to gh."""
    archive = dest / asset
    url = f"https://github.com/{REPO}/releases/download/{tag}/{asset}"
    attempts = []
    if shutil.which("curl", path=env.get("PATH")):
        attempts.append(["curl", "-fSL", "--retry", "2", "-o", str(archive), url])
    if shutil.which("gh", path=env.get("PATH")):
        attempts.append(
            ["gh", "release", "download", tag, "--repo", REPO, "-p", asset, "-D", str(dest)]
        )
    if not attempts:
        raise FetchError("neither curl nor gh is available to download the release asset")
    failures: list[str] = []
    for command in attempts:
        done = _run(command, cwd=dest, env=env, timeout=_DOWNLOAD_TIMEOUT_SECONDS)
        if done.returncode == 0 and archive.is_file():
            return archive
        failures.append(f"{command[0]} failed ({done.returncode}): {done.stderr.strip()[-300:]}")
    raise FetchError("; ".join(failures))


def _published_digest(tag: str, asset: str, dest: Path, env: Mapping[str, str]) -> str | None:
    """The sha256 GitHub records for the asset, or None when it cannot be read."""
    if not shutil.which("gh", path=env.get("PATH")):
        return None
    query = f'.assets[] | select(.name == "{asset}") | .digest'
    done = _run(
        ["gh", "api", f"repos/{REPO}/releases/tags/{tag}", "-q", query],
        cwd=dest,
        env=env,
        timeout=_API_TIMEOUT_SECONDS,
    )
    digest = done.stdout.strip() if done.returncode == 0 else ""
    return digest.removeprefix("sha256:") if digest.startswith("sha256:") else None


def _unpack(archive: Path, dest: Path) -> Path:
    """Unpack and normalize to exactly ``<dest>/codex``, executable."""
    if archive.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive) as bundle:
            names = [m.name for m in bundle.getmembers() if m.isfile()]
            name = _pick(archive, names)
            bundle.extract(name, dest, filter="data")
    else:
        with zipfile.ZipFile(archive) as bundle:
            name = _pick(archive, bundle.namelist())
            bundle.extract(name, dest)
    binary, extracted = dest / "codex", dest / name
    if extracted.resolve() != binary.resolve():
        binary.unlink(missing_ok=True)
        extracted.resolve().rename(binary)
    binary.chmod(0o755)
    return binary


def _pick(archive: Path, names: list[str]) -> str:
    exact = [name for name in names if Path(name).name == "codex"]
    fuzzy = [name for name in names if "codex" in Path(name).name]
    if not (exact or fuzzy):
        raise FetchError(f"{archive.name} contains no codex binary (files: {sorted(names)})")
    return (exact or fuzzy)[0]


def fetch(version: str, dest: Path, env: Mapping[str, str]) -> Path:
    """Download ``version`` into ``dest`` and return ``<dest>/codex``.

    The archive must match the digest GitHub publishes for it whenever that can be read.
    """
    tag, asset = release_tag(version), asset_name()
    dest.mkdir(parents=True, exist_ok=True)
    archive = _download(tag, asset, dest, env)
    published = _published_digest(tag, asset, dest, env)
    if published is not None and published != (actual := sha256_of(archive)):
        raise FetchError(
            f"{asset} sha256 {actual} does not match the digest GitHub publishes for {tag} "
            f"({published}); refusing to run an unverified binary"
        )
    return _unpack(archive, dest)
