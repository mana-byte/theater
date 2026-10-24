"""Release channels: what each adapter discovers and how it installs one exact release."""

from __future__ import annotations

import json
import stat
import subprocess
import tarfile
import urllib.error
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.harness_compat import github
from scripts.harness_compat.adapters import ADAPTER_NAMES, codex_release, load_adapter, npm
from scripts.harness_compat.adapters import codex as codex_adapter
from scripts.harness_compat.adapters import vibe as vibe_adapter
from scripts.harness_compat.contract import InstallFailed
from scripts.harness_compat.policy import load_policy, policy_relative
from scripts.harness_compat.sandbox import make_sandbox
from tests.support.paths import REPO_ROOT

Run = Callable[..., subprocess.CompletedProcess[str]]


def _done(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_the_adapters_are_the_shipped_plugins_each_with_a_policy() -> None:
    assert ADAPTER_NAMES == ("claude", "codex", "opencode", "pi", "vibe")
    for name in ADAPTER_NAMES:
        adapter = load_adapter(name)
        assert adapter.name == name
        assert (REPO_ROOT / policy_relative(name).parent / "manifest.py").is_file()
        assert load_policy(REPO_ROOT, name).minimum


def test_there_is_no_opencode_alias_or_1x_line() -> None:
    with pytest.raises(KeyError, match="opencode2"):
        load_adapter("opencode2")


def _registry(monkeypatch: pytest.MonkeyPatch, **versions: list[str]) -> list[str]:
    asked: list[str] = []

    def npm_versions(package: str) -> list[str]:
        asked.append(package)
        return versions["npm"]

    monkeypatch.setattr(github, "npm_versions", npm_versions)
    monkeypatch.setattr(
        github, "pypi_versions", lambda package: asked.append(package) or versions["pypi"]
    )
    return asked


@pytest.mark.parametrize(
    ("name", "package"),
    [
        ("claude", "@anthropic-ai/claude-code"),
        ("opencode", "@opencode/cli"),
        ("pi", "@earendil-works/pi-coding-agent"),
    ],
)
def test_npm_harnesses_discover_every_stable_release_of_their_package(
    monkeypatch: pytest.MonkeyPatch, name: str, package: str
) -> None:
    asked = _registry(monkeypatch, npm=["1.0.0", "2.0.0", "10.0.0"])

    assert load_adapter(name).discover() == ["1.0.0", "2.0.0", "10.0.0"]
    assert asked == [package]


def test_vibe_discovers_from_pypi(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _registry(monkeypatch, pypi=["3.0.2"])

    assert load_adapter("vibe").discover() == ["3.0.2"]
    assert asked == ["mistral-vibe"]


def _fake_npm(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, files: Callable[[list[str]], None]
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(command)
        files(command)
        return _done(returncode, stderr="E404 not found" if returncode else "")

    monkeypatch.setattr(npm, "run_group", run)
    return calls


def _write_bin(command: list[str], name: str, content: str = "#!/bin/sh\n") -> None:
    prefix = Path(command[command.index("--prefix") + 1])
    target = prefix / "node_modules" / ".bin" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    target.chmod(0o755)


@pytest.mark.parametrize(
    ("name", "package", "directory"),
    [("claude", "@anthropic-ai/claude-code", "npm"), ("opencode", "@opencode/cli", "npm")],
)
def test_an_npm_release_is_installed_by_exact_version_and_exposed_on_the_path(
    monkeypatch: pytest.MonkeyPatch, name: str, package: str, directory: str
) -> None:
    calls = _fake_npm(monkeypatch, files=lambda command: _write_bin(command, name))

    with make_sandbox(name) as sb:
        binary = load_adapter(name).install(sb, "2.1.4")

        assert binary == sb.bin_dir / name and binary.is_symlink()
        assert binary.resolve() == (sb.root / directory / "node_modules/.bin" / name).resolve()
    assert f"{package}@2.1.4" in calls[0] and "--prefix" in calls[0]


def test_a_failing_npm_install_is_an_install_failure_with_npms_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_npm(monkeypatch, returncode=1, files=lambda command: None)

    with make_sandbox("claude") as sb, pytest.raises(InstallFailed, match="E404 not found"):
        load_adapter("claude").install(sb, "9.9.9")


def test_an_install_that_produced_no_binary_is_an_install_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_npm(monkeypatch, files=lambda command: None)

    with make_sandbox("opencode") as sb, pytest.raises(InstallFailed, match="no opencode"):
        load_adapter("opencode").install(sb, "2.1.4")


def _pi_package(sb_bin: Path, document: dict) -> None:
    package = sb_bin / "node_modules/@earendil-works/pi-coding-agent"
    package.mkdir(parents=True, exist_ok=True)
    (package / "package.json").write_text(json.dumps(document))
    (package / "cli.js").write_text("#!/usr/bin/env node\n")


def test_pi_is_installed_once_as_the_stock_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "bin": {"pi": "cli.js"},
        "dependencies": {"@earendil-works/pi-ai": "^0.84.4", "left-pad": "^1.0.0"},
    }
    calls = _fake_npm(
        monkeypatch,
        files=lambda command: _pi_package(Path(command[command.index("--prefix") + 1]), document),
    )

    with make_sandbox("pi") as sb:
        binary = load_adapter("pi").install(sb, "0.84.4")

        assert binary == sb.bin_dir / "pi" and binary.resolve().name == "cli.js"
    assert len(calls) == 1
    assert calls[0][-1] == "@earendil-works/pi-coding-agent@0.84.4"
    assert "--ignore-scripts" not in calls[0] and "--no-save" not in calls[0]


def test_pi_without_a_valid_package_bin_is_an_install_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_npm(
        monkeypatch,
        files=lambda command: _pi_package(
            Path(command[command.index("--prefix") + 1]), {"bin": {}}
        ),
    )

    with make_sandbox("pi") as sb, pytest.raises(InstallFailed, match="no valid pi package bin"):
        load_adapter("pi").install(sb, "0.84.4")


@pytest.mark.parametrize("version", ["3.0.2", "3.1.0"])
def test_vibe_pins_python_314_for_every_supported_release(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    seen: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        seen.append(command)
        (Path(kwargs["env"]["UV_TOOL_BIN_DIR"]) / "vibe").write_text("#!/bin/sh\n")  # type: ignore[index]
        return _done()

    monkeypatch.setattr(vibe_adapter, "run_group", run)
    monkeypatch.setattr(vibe_adapter.shutil, "which", lambda *a, **k: "/usr/bin/uv")

    with make_sandbox("vibe") as sb:
        binary = load_adapter("vibe").install(sb, version)
        assert binary == sb.bin_dir / "vibe"
    assert seen[0][seen[0].index("--python") + 1] == "3.14"
    assert seen[0][-1] == f"mistral-vibe=={version}"


def test_vibe_without_uv_or_with_a_failed_install_is_an_install_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vibe_adapter.shutil, "which", lambda *a, **k: None)
    with make_sandbox("vibe") as sb, pytest.raises(InstallFailed, match="uv is unavailable"):
        load_adapter("vibe").install(sb, "3.0.2")

    monkeypatch.setattr(vibe_adapter.shutil, "which", lambda *a, **k: "/usr/bin/uv")
    monkeypatch.setattr(
        vibe_adapter, "run_group", lambda *a, **k: _done(1, stderr="no matching distribution")
    )
    with make_sandbox("vibe") as sb, pytest.raises(InstallFailed, match="no matching distribution"):
        load_adapter("vibe").install(sb, "3.0.2")


def test_codex_discovery_falls_back_to_public_tags_when_the_api_is_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def limited(*args: object, **kwargs: object) -> list[str]:
        raise urllib.error.URLError("rate limited")

    listing = "\n".join(
        f"abc\trefs/tags/{tag}"
        for tag in ("rust-v0.164.0", "rust-v0.163.0", "rust-v0.165.0-alpha.1", "v1.0.0", "other")
    )
    monkeypatch.setattr(github, "github_release_versions", limited)
    monkeypatch.setattr(codex_adapter, "run_group", lambda *a, **k: _done(0, stdout=listing))

    assert load_adapter("codex").discover() == ["0.163.0", "0.164.0"]


def test_codex_tags_without_a_stable_release_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex_adapter, "run_group", lambda *a, **k: _done(0, stdout="abc\trefs/tags/v1\n")
    )

    with pytest.raises(RuntimeError, match="no stable rust-v releases"):
        codex_adapter._public_git_tags()


def test_codex_install_fetches_the_exact_release_into_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, Path, dict]] = []

    def fetch(version: str, dest: Path, env: dict) -> Path:
        seen.append((version, dest, env))
        return dest / "codex"

    monkeypatch.setattr(codex_release, "fetch", fetch)
    monkeypatch.setenv("GITHUB_TOKEN", "digest-token")

    with make_sandbox("codex") as sb:
        assert load_adapter("codex").install(sb, "0.164.0") == sb.bin_dir / "codex"
        assert seen[0][:2] == ("0.164.0", sb.bin_dir)
        assert seen[0][2]["GITHUB_TOKEN"] == "digest-token"
        assert "GITHUB_TOKEN" not in sb.env, "the CLI under test never sees the runner's token"


@pytest.mark.parametrize("error", [codex_release.FetchError("asset missing"), OSError("disk full")])
def test_a_codex_fetch_that_fails_is_an_install_failure(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fetch(version: str, dest: Path, env: dict) -> Path:
        raise error

    monkeypatch.setattr(codex_release, "fetch", fetch)

    with make_sandbox("codex") as sb, pytest.raises(InstallFailed, match=str(error)):
        load_adapter("codex").install(sb, "0.164.0")


def _tar(path: Path, member: str) -> Path:
    payload = path.parent / "payload"
    payload.write_text("#!/bin/sh\n")
    with tarfile.open(path, "w:gz") as bundle:
        bundle.add(payload, arcname=member)
    return path


def test_the_release_archive_is_unpacked_to_an_executable_codex(tmp_path: Path) -> None:
    archive = _tar(tmp_path / "codex-x.tar.gz", "codex-aarch64-apple-darwin")

    binary = codex_release._unpack(archive, tmp_path)

    assert binary == tmp_path / "codex" and binary.stat().st_mode & stat.S_IXUSR


def test_a_zip_release_is_unpacked_too(tmp_path: Path) -> None:
    archive = tmp_path / "codex.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("codex", "#!/bin/sh\n")

    assert codex_release._unpack(archive, tmp_path).read_text() == "#!/bin/sh\n"


def test_an_archive_without_a_codex_binary_is_refused(tmp_path: Path) -> None:
    archive = _tar(tmp_path / "codex-x.tar.gz", "README")

    with pytest.raises(codex_release.FetchError, match="contains no codex binary"):
        codex_release._unpack(archive, tmp_path)


def _fake_download(
    monkeypatch: pytest.MonkeyPatch, archive: Path, *, published: str | None
) -> None:

    def download(tag: str, asset: str, dest: Path, env: dict) -> Path:
        return archive

    monkeypatch.setattr(codex_release, "asset_name", lambda: "asset.tar.gz")
    monkeypatch.setattr(codex_release, "_download", download)
    monkeypatch.setattr(codex_release, "_published_digest", lambda tag, asset, dest, env: published)


def test_a_digest_mismatch_refuses_an_unverified_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_download(monkeypatch, _tar(tmp_path / "asset.tar.gz", "codex"), published="0" * 64)

    with pytest.raises(codex_release.FetchError, match="does not match the digest"):
        codex_release.fetch("0.164.0", tmp_path / "dest", {})


@pytest.mark.parametrize("matches", [True, None])
def test_a_matching_or_unpublished_digest_unpacks_the_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, matches: bool | None
) -> None:
    archive = _tar(tmp_path / "asset.tar.gz", "codex")
    published = codex_release.sha256_of(archive) if matches else None
    _fake_download(monkeypatch, archive, published=published)

    binary = codex_release.fetch("0.164.0", tmp_path / "dest", {})

    assert binary == tmp_path / "dest" / "codex" and binary.is_file()


def test_the_download_falls_back_from_curl_to_gh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tried: list[str] = []

    def run(
        command: list[str], *, cwd: Path, env: dict, timeout: float
    ) -> subprocess.CompletedProcess:
        tried.append(command[0])
        if command[0] == "gh":
            (tmp_path / "asset.tar.gz").write_text("x")
        return _done(1 if command[0] == "curl" else 0, stderr="404")

    monkeypatch.setattr(codex_release, "_run", run)
    monkeypatch.setattr(codex_release.shutil, "which", lambda name, path=None: f"/bin/{name}")

    assert (
        codex_release._download("rust-v1", "asset.tar.gz", tmp_path, {})
        == tmp_path / "asset.tar.gz"
    )
    assert tried == ["curl", "gh"]


def test_the_download_names_every_failure_or_the_missing_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(codex_release.shutil, "which", lambda name, path=None: None)
    with pytest.raises(codex_release.FetchError, match="neither curl nor gh"):
        codex_release._download("rust-v1", "asset.tar.gz", tmp_path, {})

    monkeypatch.setattr(codex_release.shutil, "which", lambda name, path=None: f"/bin/{name}")
    monkeypatch.setattr(codex_release, "_run", lambda *a, **k: _done(22, stderr="HTTP 404"))
    with pytest.raises(codex_release.FetchError, match=r"curl failed \(22\).*gh failed \(22\)"):
        codex_release._download("rust-v1", "asset.tar.gz", tmp_path, {})


def test_an_unmapped_platform_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_release.platform, "system", lambda: "Plan9")

    with pytest.raises(codex_release.FetchError, match="no codex release asset"):
        codex_release.asset_name()
