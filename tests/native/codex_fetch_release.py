"""Fetch an exact codex-cli release binary for offline qualification runs.

Downloads the matching GitHub release asset for ``--version`` into ``--dest``,
unpacks it to ``<dest>/codex``, verifies the binary prints exactly
``codex-cli <version>``, and records the sha256 of both the archive and the
binary. Nothing is installed globally and no system path is touched; the
caller points qualification runs at ``<dest>/codex`` explicitly.

    uv run python tests/native/codex_fetch_release.py --version 0.154.0 \\
        --dest /tmp/codex-qualify/bin

Test helper only: no production code may import it.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

REPO = "openai/codex"
DOWNLOAD_TIMEOUT_SECONDS = 600.0
VERSION_PROBE_TIMEOUT_SECONDS = 30.0

#: Release asset per (os, arch); fail closed on anything unmapped.
ASSET_BY_PLATFORM: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "codex-aarch64-apple-darwin.tar.gz",
    ("darwin", "x86_64"): "codex-x86_64-apple-darwin.tar.gz",
    ("linux", "x86_64"): "codex-x86_64-unknown-linux-musl.tar.gz",
    ("linux", "aarch64"): "codex-aarch64-unknown-linux-musl.tar.gz",
}


class FetchError(RuntimeError):
    """The fetcher refused to proceed; the message says what to do."""


def release_tag(version: str) -> str:
    return f"rust-v{version}"


def asset_name(version: str) -> str:
    key = (platform.system().lower(), platform.machine())
    asset = ASSET_BY_PLATFORM.get(key)
    if asset is None:
        raise FetchError(
            f"no codex release asset mapped for {key}; qualify {version} on a mapped platform"
        )
    return asset


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise FetchError(f"{' '.join(command)} did not finish within {timeout}s") from error
    except OSError as error:
        raise FetchError(f"could not run {command[0]}: {error}") from error


def download_asset(tag: str, asset: str, dest: Path) -> Path:
    """Download via gh when available, else curl the release URL directly."""
    archive = dest / asset
    if shutil.which("gh") is not None:
        completed = _run(
            ["gh", "release", "download", tag, "--repo", REPO, "-p", asset, "-D", str(dest)],
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        if completed.returncode == 0 and archive.is_file():
            return archive
        detail = (completed.stderr or completed.stdout or "").strip()[-400:]
        raise FetchError(f"gh release download failed ({completed.returncode}): {detail}")
    if shutil.which("curl") is None:
        raise FetchError("neither gh nor curl is available to download the release asset")
    url = f"https://github.com/{REPO}/releases/download/{tag}/{asset}"
    completed = _run(
        ["curl", "-fSL", "--retry", "2", "-o", str(archive), url],
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0 or not archive.is_file():
        detail = (completed.stderr or completed.stdout or "").strip()[-400:]
        raise FetchError(f"curl download of {url} failed ({completed.returncode}): {detail}")
    return archive


def sigstore_assets(tag: str) -> list[str]:
    """Names of the release's sigstore bundles, when gh can list them."""
    if shutil.which("gh") is None:
        return []
    completed = _run(
        ["gh", "api", f"repos/{REPO}/releases/tags/{tag}", "-q", ".assets[].name"],
        timeout=60.0,
    )
    if completed.returncode != 0:
        return []
    return sorted(name for name in completed.stdout.splitlines() if name.endswith(".sigstore"))


def unpack(archive: Path, dest: Path) -> Path:
    """Unpack and normalize to exactly ``<dest>/codex``, executable."""
    extracted = (
        _unpack_tar(archive, dest)
        if archive.name.endswith((".tar.gz", ".tgz"))
        else _unpack_zip(archive, dest)
    )
    binary = dest / "codex"
    if extracted.resolve() != binary.resolve():
        if binary.exists():
            binary.unlink()
        extracted.resolve().rename(binary)
    binary.chmod(0o755)
    return binary


def _unpack_tar(archive: Path, dest: Path) -> Path:
    with tarfile.open(archive) as bundle:
        members = [m for m in bundle.getmembers() if m.isfile()]
        names = sorted(m.name for m in members)
        exact = [m for m in members if Path(m.name).name == "codex"]
        fuzzy = [m for m in members if "codex" in Path(m.name).name]
        if not (exact or fuzzy):
            raise FetchError(f"{archive.name} contains no codex binary (files: {names})")
        member = (exact or fuzzy)[0]
        bundle.extract(member, dest, filter="data")
        return dest / member.name


def _unpack_zip(archive: Path, dest: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        names = sorted(bundle.namelist())
        exact = [n for n in names if Path(n).name == "codex"]
        fuzzy = [n for n in names if "codex" in Path(n).name]
        if not (exact or fuzzy):
            raise FetchError(f"{archive.name} contains no codex binary (files: {names})")
        name = (exact or fuzzy)[0]
        bundle.extract(name, dest)
        return dest / name


def verify_version(binary: Path, version: str) -> str:
    completed = _run([str(binary), "--version"], timeout=VERSION_PROBE_TIMEOUT_SECONDS)
    output = f"{completed.stdout}{completed.stderr}".strip()
    if completed.returncode != 0 or output != f"codex-cli {version}":
        raise FetchError(
            f"{binary} --version printed {output!r}; the release must exactly "
            f"report codex-cli {version} before any qualification run"
        )
    return output


def require_usable_dest(dest: Path, *, force: bool) -> None:
    if dest.exists():
        if not dest.is_dir():
            raise FetchError(f"{dest} exists and is not a directory")
        if any(dest.iterdir()) and not force:
            raise FetchError(
                f"refusing to fetch into non-empty {dest}; pass --force or a fresh directory"
            )
        return
    dest.mkdir(parents=True)


def fetch_release(version: str, dest: Path, *, force: bool = False, tag: str | None = None) -> int:
    resolved_tag = tag if tag is not None else release_tag(version)
    asset = asset_name(version)
    require_usable_dest(dest, force=force)
    print(f"release      : {REPO} tag {resolved_tag}")
    print(f"asset        : {asset}")
    print(f"sigstore     : {sigstore_assets(resolved_tag) or 'none listed'}")
    archive = download_asset(resolved_tag, asset, dest)
    archive_sha = sha256_of(archive)
    print(f"archive      : {archive}")
    print(f"archive sha256: {archive_sha}")
    binary = unpack(archive, dest)
    print(f"binary       : {binary}")
    print(f"binary sha256: {sha256_of(binary)}")
    print(f"version      : {verify_version(binary, version)}")
    print("next         : point --codex / THEATER_CODEX_NATIVE_BIN at this binary")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex_fetch_release",
        description="Fetch and verify an exact codex-cli release binary (never installs).",
    )
    parser.add_argument("--version", required=True, help="exact release, e.g. 0.154.0")
    parser.add_argument("--dest", required=True, type=Path, help="fresh directory for the binary")
    parser.add_argument("--tag", help="override the rust-v<version> release tag")
    parser.add_argument("--force", action="store_true", help="allow a non-empty destination")
    args = parser.parse_args(argv)
    try:
        return fetch_release(args.version, args.dest, force=args.force, tag=args.tag)
    except (FetchError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
