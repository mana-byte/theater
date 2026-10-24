"""The evidence digest covers what the live suite really exercises, and nothing it never runs."""

from __future__ import annotations

from pathlib import Path

from scripts.harness_compat.inputs import adapter_digest, qualification_inputs


class SuiteAdapter:
    name = "acme"
    green_tests = ("tests/conformance/live",)
    consistency_tests: tuple[str, ...] = ()


class OldAdapter:
    name = "acme"
    green_tests = ("tests/compat/test_acme.py::test_real",)
    consistency_tests: tuple[str, ...] = ()


def _write(root: Path, relative: str, text: str = "x = 1\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _tree(root: Path) -> None:
    _write(root, "scripts/harness_compat/contract.py")
    _write(root, "scripts/harness_compat/adapters/acme.py")
    _write(root, "theater/__init__.py", "")
    _write(root, "theater/harness/builtin/plugins/acme/manifest.py")
    _write(root, "tests/conformance/live/test_x.py", "import tests.support.live_rig\n")
    _write(root, "tests/support/live_rig.py", "x = 1\n")
    _write(root, "tests/support/compat_probe/server.py", "probe = 1\n")
    _write(root, "tests/compat/test_acme.py", "def test_real(): pass\n")
    _write(root, "theater/cli/__main__.py", "from theater.daemon import server\n")
    _write(root, "theater/daemon/__init__.py", "")
    _write(root, "theater/daemon/server.py", "from theater.daemon import observer\n")
    _write(root, "theater/daemon/observer.py", "observed = 1\n")
    _write(root, "theater/daemon/migrations/versions/0001.py", "revision = 1\n")
    _write(root, "theater/skills/builtin/pair/SKILL.md", "skill\n")
    _write(root, "packages/regie/src/regie/__init__.py", "")
    _write(root, "packages/regie/src/regie/__main__.py", "from regie import entrypoint\n")
    _write(root, "packages/regie/src/regie/entrypoint.py", "from regie import bridge, app\n")
    _write(root, "packages/regie/src/regie/bridge/__init__.py", "from regie.tmux import source\n")
    _write(root, "packages/regie/src/regie/tmux/__init__.py", "")
    _write(root, "packages/regie/src/regie/tmux/source.py", "tmux = 1\n")
    _write(root, "packages/regie/src/regie/app.py", "from regie.controllers import actions\n")
    _write(root, "packages/regie/src/regie/controllers/__init__.py", "")
    _write(root, "packages/regie/src/regie/controllers/actions.py", "ui = 1\n")


def _names(root: Path, adapter: object) -> set[str]:
    return {
        item.relative_to(root).as_posix() for item in qualification_inputs(root, "acme", adapter)
    }  # type: ignore[arg-type]


def test_a_suite_adapter_digests_the_daemon_the_bridge_and_the_runtime_data(tmp_path: Path) -> None:
    _tree(tmp_path)

    names = _names(tmp_path, SuiteAdapter())

    assert {
        "theater/daemon/server.py",
        "theater/daemon/observer.py",
        "theater/daemon/migrations/versions/0001.py",
        "theater/skills/builtin/pair/SKILL.md",
        "packages/regie/src/regie/bridge/__init__.py",
        "packages/regie/src/regie/tmux/source.py",
        "tests/support/compat_probe/server.py",
        "tests/support/live_rig.py",
    } <= names


def test_the_terminal_ui_is_left_out_of_the_exercised_closure(tmp_path: Path) -> None:
    _tree(tmp_path)

    names = _names(tmp_path, SuiteAdapter())

    assert "packages/regie/src/regie/app.py" not in names
    assert "packages/regie/src/regie/controllers/actions.py" not in names


def test_changing_an_exercised_module_changes_the_digest_but_a_ui_change_does_not(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    adapter = SuiteAdapter()
    before = adapter_digest(tmp_path, "acme", adapter)  # type: ignore[arg-type]

    _write(tmp_path, "packages/regie/src/regie/controllers/actions.py", "ui = 2\n")
    assert adapter_digest(tmp_path, "acme", adapter) == before  # type: ignore[arg-type]

    _write(tmp_path, "theater/daemon/observer.py", "observed = 2\n")
    assert adapter_digest(tmp_path, "acme", adapter) != before  # type: ignore[arg-type]


def test_an_adapter_whose_proof_is_not_the_suite_ignores_the_daemon_and_the_bridge(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)

    names = _names(tmp_path, OldAdapter())

    assert "theater/daemon/server.py" not in names
    assert "packages/regie/src/regie/bridge/__init__.py" not in names
