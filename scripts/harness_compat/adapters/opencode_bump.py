"""OpenCode 1.x gate reading and the deterministic ceiling bump, pure text only.

The gate lives in ``runtime_plan.py`` (MIN/MAX tuples); the bump admits a
contiguous rung of newly green releases by raising the exclusive ceiling to
last-green-plus-one-patch and every textual upper bound to the last green.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..contract import VerifiedSet, load_evidenced, version_key

RUNTIME_PLAN = Path("theater/harness/builtin/plugins/opencode/runtime_plan.py")
SERVER_PLAN = Path("theater/harness/builtin/plugins/opencode/server_plan.py")
MANIFEST = Path("theater/harness/builtin/plugins/opencode/manifest.py")
STOCK_UI_TEST = Path("tests/test_opencode_stock_ui.py")
SERVER_RUNTIME_TEST = Path("tests/test_opencode_server_runtime.py")
SERVER_PLAN_TEST = Path("tests/test_opencode_server_plan.py")
SERVER_PROOF_TEST = Path("tests/test_opencode_server_proof.py")
DASHBOARD_TEST = Path("packages/regie/tests/test_dashboard_rich.py")
OPCODE_DOC = Path("docs/native-interaction/opencode.md")

#: Every file a ceiling bump touches; historical prose inside them is left alone.
COUPLED_FILES: tuple[Path, ...] = (
    RUNTIME_PLAN,
    SERVER_PLAN,
    MANIFEST,
    STOCK_UI_TEST,
    SERVER_RUNTIME_TEST,
    SERVER_PLAN_TEST,
    SERVER_PROOF_TEST,
    DASHBOARD_TEST,
    OPCODE_DOC,
)

_GATE_NAMES = (
    "OPENCODE_TUI_MIN_VERSION",
    "OPENCODE_TUI_MAX_VERSION",
    "OPENCODE_SERVER_MIN_VERSION",
    "OPENCODE_SERVER_MAX_VERSION",
)
_WINDOW_TABLE = re.compile(r'\[\("1\.17\.14", False\), \("1\.17\.15", True\), (?P<rows>[^]]*)\],')
#: Per coupled file, the literal (old, new) pairs; the window table is rebuilt separately.
Substitutions = tuple[dict[Path, list[tuple[str, str]]], str]


def _render(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _tuple_text(version: str) -> str:
    major, minor, patch = version_key(version)
    return f"({major}, {minor}, {patch})"


def previous_patch(version: str) -> str:
    """The release just below ``version``; the textual bound of an exclusive ceiling."""
    major, minor, patch = version_key(version)
    if patch == 0:
        raise ValueError(f"no previous patch release for {version!r}")
    return f"{major}.{minor}.{patch - 1}"


def next_patch(version: str) -> str:
    """The exclusive ceiling that admits ``version`` as the last green release."""
    major, minor, patch = version_key(version)
    return f"{major}.{minor}.{patch + 1}"


def read_gate(root: Path) -> tuple[str, str]:
    """(floor, exclusive ceiling) parsed from the tree at ``root``; never imports theater."""
    text = (root / RUNTIME_PLAN).read_text(encoding="utf-8")
    gate: dict[str, tuple[int, int, int]] = {}
    for name in _GATE_NAMES:
        match = re.search(rf"^{name} = \((\d+), (\d+), (\d+)\)$", text, re.M)
        if match is None:
            raise ValueError(f"the OpenCode gate is unreadable: {name} missing from {RUNTIME_PLAN}")
        gate[name] = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if gate["OPENCODE_TUI_MIN_VERSION"] != gate["OPENCODE_SERVER_MIN_VERSION"] or (
        gate["OPENCODE_TUI_MAX_VERSION"] != gate["OPENCODE_SERVER_MAX_VERSION"]
    ):
        raise ValueError("the OpenCode TUI and server gates disagree in runtime_plan.py")
    tui_min, tui_max = gate["OPENCODE_TUI_MIN_VERSION"], gate["OPENCODE_TUI_MAX_VERSION"]
    floor, ceiling = _render(tui_min), _render(tui_max)
    manifest = (root / MANIFEST).read_text(encoding="utf-8")
    if f">={floor},<{ceiling} || " not in manifest:
        raise ValueError(f"manifest.py does not declare the gate range >= {floor},< {ceiling}")
    return floor, ceiling


def verified_set(root: Path) -> VerifiedSet:
    """The gate under ``root``: an inclusive floor, an exclusive ceiling, no known-bad."""
    floor, ceiling = read_gate(root)
    return VerifiedSet(
        kind="range",
        floor=floor,
        ceiling=ceiling,
        known_bad=frozenset(),
        evidenced=load_evidenced(root, "opencode"),
    )


def _substitutions(floor: str, ceiling: str, greens: list[str]) -> Substitutions:
    """Literal old→new pairs per file plus the rebuilt window-table rows.

    Every anchor must exist in the tree or the bump fails closed.
    """
    old_bound, last_green = previous_patch(ceiling), greens[-1]
    new_ceiling = next_patch(last_green)
    plain_range = (f"{floor}-{old_bound}", f"{floor}-{last_green}")
    backticked_range = (f"`{floor}`–`{old_bound}`", f"`{floor}`–`{last_green}`")
    window_rows = (
        ", ".join(f'("{green}", True)' for green in greens) + f', ("{new_ceiling}", False)'
    )
    pairs: dict[Path, list[tuple[str, str]]] = {
        RUNTIME_PLAN: [
            plain_range,
            (
                f"OPENCODE_TUI_MAX_VERSION = {_tuple_text(ceiling)}",
                f"OPENCODE_TUI_MAX_VERSION = {_tuple_text(new_ceiling)}",
            ),
            (
                f"OPENCODE_SERVER_MAX_VERSION = {_tuple_text(ceiling)}",
                f"OPENCODE_SERVER_MAX_VERSION = {_tuple_text(new_ceiling)}",
            ),
            (f"pinned to stock {old_bound}", f"pinned to stock {last_green}"),
        ],
        SERVER_PLAN: [plain_range],
        MANIFEST: [(f",<{ceiling} || ", f",<{new_ceiling} || ")],
        STOCK_UI_TEST: [plain_range],
        SERVER_RUNTIME_TEST: [
            (f'"health-version"] = "{ceiling}"', f'"health-version"] = "{new_ceiling}"')
        ],
        SERVER_PLAN_TEST: [],  # the 1.x window table is rebuilt, not paired
        SERVER_PROOF_TEST: [
            (f'_PINNED_VERSION = "{old_bound}"', f'_PINNED_VERSION = "{last_green}"')
        ],
        OPCODE_DOC: [
            backticked_range,
            (f"pinned to stock `{old_bound}`", f"pinned to stock `{last_green}`"),
        ],
        DASHBOARD_TEST: [
            (
                f'">={floor},<{ceiling} || >=2.0.6,<2.1.0"',
                f'">={floor},<{new_ceiling} || >=2.0.6,<2.1.0"',
            ),
            (f'"{floor}–{old_bound} · 2.0.6–2.0.x"', f'"{floor}–{last_green} · 2.0.6–2.0.x"'),
        ],
    }
    return pairs, window_rows


def _apply_window_table(text: str, window_rows: str) -> str:
    match = _WINDOW_TABLE.search(text)
    if match is None:
        raise ValueError("cannot find the 1.x window table in test_opencode_server_plan.py")
    return _WINDOW_TABLE.sub(
        f'[("1.17.14", False), ("1.17.15", True), {window_rows}],', text, count=1
    )


def apply_bump(root: Path, greens: list[str]) -> list[Path]:
    """Admit ascending greens at or above the current ceiling under ``root``.

    Deterministic and idempotent: a second call with the same greens is a no-op
    that changes nothing. Returns the changed paths relative to ``root``.
    """
    if not greens:
        return []
    keys = [version_key(green) for green in greens]
    if any(key[0] != 1 for key in keys):
        raise ValueError(f"the OpenCode v1 adapter only admits 1.x releases: {greens}")
    if keys != sorted(keys) or len(set(greens)) != len(greens):
        raise ValueError(f"greens must be unique and ascending: {greens}")
    floor, ceiling = read_gate(root)
    floor_key, ceiling_key = version_key(floor), version_key(ceiling)
    if all(floor_key <= key < ceiling_key for key in keys):
        return []  # every green is already admitted: idempotent no-op
    if keys[0] < ceiling_key:
        raise ValueError(
            f"green releases must start at or above the current ceiling {ceiling!r}; "
            f"got {greens[0]!r}"
        )

    rules, window_rows = _substitutions(floor, ceiling, greens)
    changed: list[Path] = []
    for relative, pairs in rules.items():
        path = root / relative
        original = path.read_text(encoding="utf-8")
        text = original
        for old, new in pairs:
            if old not in text:
                raise ValueError(f"bump anchor missing in {relative}: {old!r}")
            text = text.replace(old, new)
        if relative == SERVER_PLAN_TEST:
            text = _apply_window_table(text, window_rows)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed.append(relative)
    return sorted(changed)
