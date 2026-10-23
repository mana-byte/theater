"""Distinguish stale compatibility limits from historical version references."""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .contract import EVIDENCE_ROOT, Adapter, VerifiedSet, version_key

_SOURCE_SUFFIXES = frozenset({".py", ".ts", ".mts", ".tsx", ".js", ".mjs"})
_QUALIFIED = re.compile(r"\b(?:qualified|qualification|requires?|supported|admitted)\b", re.I)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=False
    )


def _limit_tokens(gate: VerifiedSet) -> set[str]:
    major, minor, patch = version_key(gate.ceiling)
    tokens = {gate.ceiling}
    if patch > 0:
        tokens.add(f"{major}.{minor}.{patch - 1}")
    return tokens


def _version_match(version: str, line: str) -> re.Match[str] | None:
    return re.search(rf"(?<![\d.]){re.escape(version)}(?!\.?\d)", line)


def _code_without_comment(line: str) -> str:
    stripped = line.lstrip()
    if stripped.startswith(("#", "//", "/*", "*")):
        return ""
    return re.split(r"\s(?:#|//)", line, maxsplit=1)[0]


def _is_limit(line: str, token: str, *, source: bool) -> bool:
    text = _code_without_comment(line) if source else line
    major, minor, patch = version_key(token)
    if source and re.search(rf"\(\s*{major}\s*,\s*{minor}\s*,\s*{patch}\s*\)", text):
        return True
    match = _version_match(token, text)
    if match is None:
        return False
    left, right = text[: match.start()].rstrip(" `\"'"), text[match.end() :]
    if re.search(r"(?:<=|>=|<|>)\s*$", left):
        return True
    if re.search(r"\d+\.\d+\.\d+\s*[-–]\s*$", left):
        return source or _QUALIFIED.search(text) is not None
    if re.search(r"\bthrough\s*$", left, re.I):
        return source or _QUALIFIED.search(text) is not None
    if source:
        quoted = re.escape(token)
        if re.search(rf"(?:=|==|!=|===|!==)\s*['\"`][^'\"`]*{quoted}", text):
            return True
        if re.search(
            rf"['\"`][^'\"`]*{quoted}[^'\"`]*['\"`]\s*(?:==|!=|<=|>=|<|>|in\b)",
            text,
        ):
            return True
        if _QUALIFIED.search(text) and re.search(r"[-–<>]", text):
            return True
    return bool(not source and _QUALIFIED.search(text) and re.search(r"[-–<>]", left + right))


def _old_exclusive_bound(line: str, ceiling: str, *, source: bool) -> bool:
    text = _code_without_comment(line) if source else line
    major, minor, patch = version_key(ceiling)
    literal = rf"\(\s*{major}\s*,\s*{minor}\s*,\s*{patch}\s*\)"
    if source and re.search(rf"\b[A-Z_]*MAX_VERSION\s*=\s*{literal}", text):
        return True
    if source and re.search(rf"(?<!<)<\s*{literal}", text):
        return True
    match = _version_match(ceiling, text)
    if match is None:
        return False
    left = text[: match.start()].rstrip(" `\"'")
    return re.search(r"(?<![<>=])<\s*$", left) is not None


def stale_references(adapter: Adapter, root: Path) -> tuple[list[str], list[str]]:
    """Return hard stale limits and soft historical references in changed files."""
    new_gate = adapter.verified(root)
    with tempfile.TemporaryDirectory(prefix="compat-old-gate-") as temporary:
        old_root = Path(temporary) / "tree"
        added = _git(root, "worktree", "add", "--detach", str(old_root), "HEAD")
        if added.returncode:
            return [f"cannot read old gate: {added.stderr.strip()}"], []
        try:
            old_gate = adapter.verified(old_root)
            stale = _limit_tokens(old_gate) - _limit_tokens(new_gate)
            old_ceiling = old_gate.ceiling if old_gate.ceiling != new_gate.ceiling else None
            if not stale and old_ceiling is None:
                return [], []
            paths = _git(root, "diff", "--name-only", "HEAD", "--").stdout.splitlines()
            paths.extend(
                _git(root, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
            )
            hard: list[str] = []
            soft: list[str] = []
            for name in sorted(set(paths)):
                path = root / name
                if (
                    not path.is_file()
                    or path.suffix not in _SOURCE_SUFFIXES
                    or Path(name).is_relative_to(EVIDENCE_ROOT)
                    or "docs" in path.parts
                    or path.name.startswith("README")
                ):
                    continue
                for number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    for token in sorted(stale, key=version_key):
                        if _is_limit(line, token, source=True):
                            hard.append(f"old limit {token} remains in {name}:{number}")
                        elif _version_match(token, line):
                            soft.append(f"{name}:{number} ({token})")
                    if (
                        old_ceiling
                        and old_ceiling not in stale
                        and _old_exclusive_bound(line, old_ceiling, source=True)
                    ):
                        hard.append(f"old limit {old_ceiling} remains in {name}:{number}")
            return hard, soft
        finally:
            _git(root, "worktree", "remove", "--force", str(old_root))


def stale_warnings(adapter: Adapter, root: Path, bumped: Sequence[str]) -> list[str]:
    """Historical references for PR review; these never fail a coherent bump."""
    return stale_references(adapter, root)[1] if bumped else []
