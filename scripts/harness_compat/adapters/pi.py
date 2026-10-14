"""Compatibility qualification for the published Pi coding agent."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import framework, github
from ..contract import (
    Check,
    InstallFailed,
    Result,
    Sandbox,
    VerifiedSet,
    load_evidenced,
    verdict_from_checks,
    version_key,
)

_PACKAGE = "@earendil-works/pi-coding-agent"
_PROBE = Path("theater/harness/builtin/plugins/pi/runtime_probe.py")
_CONSTANTS = Path("theater/harness/builtin/plugins/pi/runtime_constants.py")
_MANIFEST = Path("theater/harness/builtin/plugins/pi/manifest.py")
_BRIDGE_TEST = Path("tests/test_pi_native_bridge.py")
_CONTROL_TEST = Path("tests/test_pi_native_control.py")
_FIXTURE_DIR = Path("tests/fixtures/pi_native_control")
_DOC = Path("docs/native-interaction/pi.md")
_COUPLED = (
    _PROBE,
    _CONSTANTS,
    _MANIFEST,
    _BRIDGE_TEST,
    _CONTROL_TEST,
    _FIXTURE_DIR / "README.md",
    _FIXTURE_DIR / "pi_active_run_interrupt_proof.mts",
    _FIXTURE_DIR / "pi_core_correlation_proof.mts",
    _FIXTURE_DIR / "pi_steer_admission_proof.mts",
    _DOC,
)
_GATE_RE = re.compile(
    r"in_080\s*=\s*\((?P<floor>[^)]+)\)\s*<=\s*parsed\s*<\s*"
    r"\((?P<bad>[^)]+)\).*?"
    r"in_qualified\s*=\s*\((?P<resume>[^)]+)\)\s*<=\s*parsed\s*<=\s*"
    r"\((?P<upper>[^)]+)\)",
    re.DOTALL,
)
_REFUSED_RE = re.compile(r'@pytest\.mark\.parametrize\("version", \[(?P<versions>[^]]+)\]\)')


def _next_patch(version: str) -> str:
    major, minor, patch = version_key(version)
    return f"{major}.{minor}.{patch + 1}"


def _tuple(version: str) -> str:
    return ", ".join(str(part) for part in version_key(version))


def _version_from_tuple(value: str) -> str:
    parts = tuple(int(part.strip()) for part in value.split(","))
    if len(parts) != 3:
        raise ValueError(f"expected a three-part Pi version tuple, got {value!r}")
    version = ".".join(str(part) for part in parts)
    version_key(version)
    return version


def _replace(text: str, old: str, new: str, path: Path) -> str:
    if old not in text:
        raise ValueError(f"expected {old!r} in {path}")
    return text.replace(old, new)


def _insert_qualified_versions(text: str, current: str, additions: Sequence[str]) -> str:
    marker = f'    "{current}",'
    lines = "".join(f'    "{version}",\n' for version in additions)
    return text.replace(f"{marker}\n", f"{marker}\n{lines}", 1)


def _update_refused_versions(text: str, upper: str, floor: str, known_bad: frozenset[str]) -> str:
    match = _REFUSED_RE.search(text)
    if match is None:
        raise ValueError(f"could not parse refused Pi versions in {_BRIDGE_TEST}")
    versions = re.findall(r'"(\d+\.\d+\.\d+)"', match.group("versions"))
    kept = [
        version
        for version in versions
        if version in known_bad
        or version_key(version) < version_key(floor)
        or version_key(version) > version_key(upper)
    ]
    kept.append(_next_patch(upper))
    rendered = ", ".join(f'"{version}"' for version in sorted(set(kept), key=version_key))
    start, end = match.span("versions")
    return f"{text[:start]}{rendered}{text[end:]}"


def _generalize_version_parsing(texts: dict[Path, str]) -> None:
    constants = texts[_CONSTANTS]
    old_token = (
        r'_VERSION_TOKEN = re.compile(r"(?<![\w.])(?P<version>0\.'
        r'(?P<minor>\d+)\.(?P<patch>\d+))(?![\w.-])")'
    )
    constants = constants.replace(
        old_token,
        "_VERSION_TOKEN = re.compile(\n"
        r'    r"(?<![\w.])(?P<version>(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+))(?![\w.-])"'
        "\n)",
    )
    texts[_CONSTANTS] = constants

    probe = texts[_PROBE]
    probe = probe.replace("stable ``0.x.y`` Pi release", "stable Pi release")
    probe = probe.replace(
        'parsed = (0, int(match.group("minor")), int(match.group("patch")))',
        'parsed = tuple(int(match.group(part)) for part in ("major", "minor", "patch"))',
    )
    probe = probe.replace(
        '"Pi --version did not report a stable 0.x.y release"',
        '"Pi --version did not report a stable release"',
    )
    texts[_PROBE] = probe

    bridge_test = texts[_BRIDGE_TEST]
    bridge_test = bridge_test.replace(
        "    # 1.0.0 is not a stable 0.x.y release at all, so no version is reported.\n"
        '    if version != "1.0.0":\n'
        "        assert refused.native_version == version\n"
        '        assert "outside the qualified range" in refused.reason',
        "    assert refused.native_version == version\n"
        '    assert "outside the qualified range" in refused.reason',
    )
    texts[_BRIDGE_TEST] = bridge_test


def _bump_fixture_gate(text: str, old: str, new: str, path: Path) -> str:
    text = _replace(
        text,
        f"({_tuple(old).replace(' ', '')})",
        f"({_tuple(new).replace(' ', '')})",
        path,
    )
    text = text.replace(r"/^0\.(\d+)\.(\d+)$/", r"/^(\d+)\.(\d+)\.(\d+)$/")
    text = text.replace(
        "const minor = Number(versionMatch[1]);\n\tconst patch = Number(versionMatch[2]);",
        "const major = Number(versionMatch[1]);\n"
        "\tconst minor = Number(versionMatch[2]);\n"
        "\tconst patch = Number(versionMatch[3]);",
    )
    text = text.replace(
        "if (minor < 80 || (minor === 80 && patch < 8)) return undefined;",
        "if (major === 0 && (minor < 80 || (minor === 80 && patch < 8))) return undefined;",
    )
    text = text.replace(
        "if (minor === 85 && patch === 0) return undefined;",
        "if (major === 0 && minor === 85 && patch === 0) return undefined;",
    )
    old_major, old_minor, old_patch = version_key(old)
    new_major, new_minor, new_patch = version_key(new)
    old_guard = (
        f"if (minor > {old_minor} || (minor === {old_minor} && patch > {old_patch})) "
        "return undefined;"
    )
    new_guard = (
        f"if (major > {new_major} ||\n"
        f"\t\t(major === {new_major} && minor > {new_minor}) ||\n"
        f"\t\t(major === {new_major} && minor === {new_minor} && patch > {new_patch}))\n"
        "\t\treturn undefined;"
    )
    if old_major != 0:
        old_guard = (
            f"if (major > {old_major} ||\n"
            f"\t\t(major === {old_major} && minor > {old_minor}) ||\n"
            f"\t\t(major === {old_major} && minor === {old_minor} && patch > {old_patch}))\n"
            "\t\treturn undefined;"
        )
    return _replace(text, old_guard, new_guard, path)


@dataclass(frozen=True, slots=True)
class PiAdapter:
    name: str = "pi"
    probe: str = "theater.harness.builtin.plugins.pi.runtime_probe:probe_pi_frontend_compatibility"
    stub_help: str = ""
    green_tests: tuple[str, ...] = (
        "tests/test_pi_native_control.py::test_pi_core_correlation_proof_holds_on_stock_pi",
        "tests/test_pi_native_control.py::test_pi_active_run_interrupt_proof_holds_on_stock_pi",
        "tests/test_pi_native_control.py::test_pi_steer_admission_proof_holds_on_stock_pi",
    )
    consistency_tests: tuple[str, ...] = (
        "tests/test_pi_native_bridge.py::test_pi_frontend_probe_accepts_qualified_versions",
        "tests/test_pi_native_bridge.py::test_pi_frontend_probe_refuses_unqualified_versions",
    )
    expected_skips: Mapping[str, str] = field(default_factory=dict)
    expected_routes: frozenset[tuple[str, str]] = frozenset()
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()

    def discover(self) -> list[str]:
        return github.npm_versions(_PACKAGE)

    def verified(self, root: Path) -> VerifiedSet:
        source = (root / _PROBE).read_text(encoding="utf-8")
        match = _GATE_RE.search(source)
        if match is None:
            raise ValueError(f"could not parse Pi compatibility gate in {_PROBE}")
        floor = _version_from_tuple(match.group("floor"))
        bad = _version_from_tuple(match.group("bad"))
        resume = _version_from_tuple(match.group("resume"))
        upper = _version_from_tuple(match.group("upper"))
        if _next_patch(bad) != resume:
            raise ValueError(f"Pi compatibility gap is not one excluded patch: {bad} to {resume}")
        return VerifiedSet(
            "range",
            floor=floor,
            ceiling=_next_patch(upper),
            known_bad=frozenset({bad}),
            evidenced=load_evidenced(root, self.name),
        )

    def install(self, sb: Sandbox, version: str) -> Path:
        command = (
            "npm",
            "install",
            "--prefix",
            str(sb.bin_dir),
            "--no-save",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            f"{_PACKAGE}@{version}",
        )
        try:
            completed = subprocess.run(
                command,
                env=dict(sb.env),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallFailed(f"npm could not install Pi {version}: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-500:]
            raise InstallFailed(f"npm could not install Pi {version}: {detail}")

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

        binary = sb.bin_dir / "pi"
        try:
            binary.symlink_to(target)
        except OSError as exc:
            raise InstallFailed(f"could not expose installed Pi {version} binary: {exc}") from exc
        return binary

    def version_line(self, version: str) -> str:
        return version

    def apply_bump(self, root: Path, greens: Sequence[str]) -> list[Path]:
        ordered = list(dict.fromkeys(greens))
        if not ordered:
            return []
        if ordered != sorted(ordered, key=version_key):
            raise ValueError("Pi green releases must be stable and ascending")

        gate = self.verified(root)
        assert gate.ceiling is not None
        current_key = version_key(gate.ceiling)
        current = f"{current_key[0]}.{current_key[1]}.{current_key[2] - 1}"
        additions = [version for version in ordered if version_key(version) > version_key(current)]
        if not additions:
            return []
        new = additions[-1]

        texts = {path: (root / path).read_text(encoding="utf-8") for path in _COUPLED}
        _generalize_version_parsing(texts)
        old_tuple, new_tuple = _tuple(current), _tuple(new)
        assert gate.floor is not None and len(gate.known_bad) == 1
        bad = next(iter(gate.known_bad))
        resume = _next_patch(bad)
        range_old = f">={gate.floor},<{bad} || >={resume},<={current}"
        range_new = f">={gate.floor},<{bad} || >={resume},<={new}"

        probe = texts[_PROBE]
        probe = _replace(probe, f"({old_tuple})", f"({new_tuple})", _PROBE)
        probe = _replace(
            probe,
            f"({old_tuple.replace(' ', '')})",
            f"({new_tuple.replace(' ', '')})",
            _PROBE,
        )
        texts[_PROBE] = _replace(probe, range_old, range_new, _PROBE)
        texts[_CONSTANTS] = _replace(texts[_CONSTANTS], current, new, _CONSTANTS)
        manifest = _replace(texts[_MANIFEST], range_old, range_new, _MANIFEST)
        texts[_MANIFEST] = _replace(manifest, f"through {current}", f"through {new}", _MANIFEST)

        bridge_test = _insert_qualified_versions(texts[_BRIDGE_TEST], current, additions)
        texts[_BRIDGE_TEST] = _update_refused_versions(bridge_test, new, gate.floor, gate.known_bad)
        texts[_CONTROL_TEST] = _replace(
            texts[_CONTROL_TEST], f"{resume}-{current}", f"{resume}-{new}", _CONTROL_TEST
        )

        for path in _COUPLED:
            if path.suffix == ".mts":
                texts[path] = _bump_fixture_gate(texts[path], current, new, path)
        readme = texts[_FIXTURE_DIR / "README.md"]
        texts[_FIXTURE_DIR / "README.md"] = _replace(
            readme, f"{resume}-{current}", f"{resume}-{new}", _FIXTURE_DIR / "README.md"
        )

        doc = _replace(texts[_DOC], range_old, range_new, _DOC)
        doc = doc.replace(
            f"{current}: all three proofs",
            f"{current}, {', '.join(additions)}: all three proofs",
            1,
        )
        texts[_DOC] = _replace(doc, f"{resume}-{current}", f"{resume}-{new}", _DOC)

        changed: list[Path] = []
        for path in _COUPLED:
            target = root / path
            if target.read_text(encoding="utf-8") != texts[path]:
                target.write_text(texts[path], encoding="utf-8")
                changed.append(path)
        return changed

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        env = dict(sb.offline_env)
        env["PATH"] = f"{binary.parent}{os.pathsep}{env.get('PATH', '')}"
        consistency = framework.run_pytest(
            root,
            self.consistency_tests,
            env=env,
            expected_skips={},
        )
        if any(check.status != "pass" for check in consistency):
            return Result(self.name, version, "tooling", consistency, sb.evidence_dir)

        proofs = framework.run_pytest(
            root,
            self.green_tests,
            env=env,
            expected_skips=self.expected_skips,
        )
        checks = consistency + proofs
        observed = {(request.method, request.path) for request in sb.mock.requests}
        unexpected = observed - self.expected_routes - self.ignorable_routes
        unhandled = {
            (request.method, request.path)
            for request in sb.mock.unhandled
            if (request.method, request.path) not in self.ignorable_routes
        }
        if unexpected or unhandled:
            detail = f"unexpected={sorted(unexpected)!r}; unhandled={sorted(unhandled)!r}"
            return Result(
                self.name,
                version,
                "tooling",
                (*checks, Check("mock routes", "error", detail)),
                sb.evidence_dir,
            )
        return Result(
            self.name,
            version,
            verdict_from_checks(proofs),
            checks,
            sb.evidence_dir,
        )


ADAPTER = PiAdapter()
