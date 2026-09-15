"""Focused tests for the codex qualification evidence collector.

The collector is evidence-only: these tests verify the deterministic helpers,
the refusal rules, and the collector's independence from Theater production
code — the allowlist can never be edited by collecting evidence.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import cast

import pytest

from tests.native import codex_qualify_runtime as collector
from tests.native.codex_native_client import AppServerProcess

SCHEMA_FILE_NAMES = (
    "ClientRequest.json",
    "ClientNotification.json",
    "ServerRequest.json",
    "ServerNotification.json",
    "JSONRPCRequest.json",
    "JSONRPCMessage.json",
)


def write_fake_codex(directory: Path, *, version: str = "0.0.0-test") -> Path:
    """A stand-in binary: answers --version and emits the six schema files."""
    binary = directory / "fake-codex"
    binary.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, sys
            from pathlib import Path
            if sys.argv[1:2] == ["--version"]:
                print("codex-cli {version}")
                raise SystemExit(0)
            if "--out" not in sys.argv:
                raise SystemExit("unexpected arguments")
            out = Path(sys.argv[sys.argv.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            for name in {SCHEMA_FILE_NAMES!r}:
                (out / name).write_text(json.dumps(
                    {{"type": "object", "properties": {{"zebra": {{}}, "ant": {{}}}}}},
                    indent=4,
                ))
            """
        )
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


class TestNormalizeJson:
    def test_sorted_keys_fixed_indent_and_stable_output(self) -> None:
        document = {"b": 1, "a": {"y": [3, 1, 2], "x": "enum value"}}
        once = collector.normalize_json(document)
        assert once == collector.normalize_json(json.loads(once))
        assert once.index('"a"') < once.index('"b"')
        assert json.loads(once)["a"]["y"] == [3, 1, 2]


class TestRedact:
    def test_roots_and_uuids_collapse_longest_root_first(self) -> None:
        nested = {
            "path": "/tmp/run-1/repo",
            "inner": ["/tmp/run-1/repo/deep/thread-12345678-1234-1234-1234-123456789012"],
            "home": str(Path.home()) + "/notes",
            "number": 7,
        }
        redacted = collector.redact(
            nested, {"/tmp/run-1": "<run-root>", str(Path.home()): "<home>"}
        )
        assert isinstance(redacted, dict)
        assert redacted["path"] == "<run-root>/repo"
        assert redacted["inner"] == ["<run-root>/repo/deep/thread-<uuid>"]
        assert redacted["home"] == "<home>/notes"
        assert redacted["number"] == 7


class TestJsonDiff:
    def test_reports_leaf_differences_deterministically(self) -> None:
        lines: list[str] = []
        collector.json_diff(
            {"a": {"b": 1}, "c": [1, 2], "d": "same"},
            {"a": {"b": 2}, "c": [1, 2, 3], "d": "same"},
            "x",
            lines,
        )
        assert lines == ["x.a.b: 1 -> 2", "x.c: array length 2 -> 3"]

    def test_reports_missing_keys_and_type_changes(self) -> None:
        lines: list[str] = []
        collector.json_diff({"gone": 1, "kept": [1]}, {"new": "v", "kept": [1]}, "x", lines)
        assert lines == [
            "x.gone: 1 -> absent",
            'x.new: absent -> "v"',
        ]
        lines.clear()
        collector.json_diff({"v": 1}, {"v": "one"}, "x", lines)
        assert lines == ['x.v: 1 -> "one"']


class TestDiffBundle:
    def test_file_level_and_content_differences_are_bounded(self, tmp_path: Path) -> None:
        baseline = tmp_path / "baseline"
        candidate = tmp_path / "candidate"
        for directory in (baseline, candidate):
            directory.mkdir()
            (directory / "protocol_schema").mkdir()
        (baseline / "gone.json").write_text("{}")
        (candidate / "added.json").write_text("{}")
        (baseline / "protocol_schema" / "ServerRequest.json").write_text('{"a": 1}')
        (candidate / "protocol_schema" / "ServerRequest.json").write_text('{"a": 2}')
        lines = collector.diff_bundle(baseline, candidate)
        assert lines == [
            "gone.json: present in baseline, missing from candidate",
            "added.json: new in candidate",
            "protocol_schema/ServerRequest.json.a: 1 -> 2",
        ]
        identical = tmp_path / "same-a"
        identical.mkdir()
        (identical / "f.json").write_text('{"k": [1, 2]}')
        assert collector.diff_bundle(identical, identical) == []

    def test_output_is_capped_at_the_documented_limit(self, tmp_path: Path) -> None:
        baseline = tmp_path / "many-a"
        candidate = tmp_path / "many-b"
        baseline.mkdir()
        candidate.mkdir()
        (baseline / "doc.json").write_text(json.dumps({f"k{i}": i for i in range(600)}))
        (candidate / "doc.json").write_text(json.dumps({f"k{i}": -i for i in range(600)}))
        lines = collector.diff_bundle(baseline, candidate)
        assert len(lines) == collector.DIFF_LINE_LIMIT
        assert lines[-1].startswith("... ")


class TestRequireFreshOutput:
    def test_refuses_non_empty_output_and_fixture_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        monkeypatch.setattr(collector, "FIXTURE_ROOT", fixtures)

        dirty = tmp_path / "dirty"
        dirty.mkdir()
        (dirty / "prior.json").write_text("{}")
        with pytest.raises(collector.QualificationError, match="never overwritten"):
            collector.require_fresh_output(dirty)

        with pytest.raises(collector.QualificationError, match="fixture tree"):
            collector.require_fresh_output(fixtures / "0.9.9")

        fresh = tmp_path / "fresh" / "nested"
        collector.require_fresh_output(fresh)
        assert fresh.is_dir()

        empty = tmp_path / "empty"
        empty.mkdir()
        collector.require_fresh_output(empty)


class TestReleaseFacts:
    def test_records_exact_binary_digest_and_version(self, tmp_path: Path) -> None:
        binary = write_fake_codex(tmp_path)
        facts = collector.resolve_release_facts(str(binary))
        assert facts.version == "0.0.0-test"
        assert facts.version_output == "codex-cli 0.0.0-test"
        assert facts.resolved_path == str(binary.resolve())
        assert facts.digest == hashlib.sha256(binary.read_bytes()).hexdigest()

    def test_facts_document_never_leaks_the_absolute_binary_path(self, tmp_path: Path) -> None:
        # tmp_path is outside Path.home(), so a leaked path would be visible.
        binary = write_fake_codex(tmp_path)
        facts = collector.resolve_release_facts(str(binary))
        document = json.dumps(collector.release_facts_document(facts))
        assert collector.release_facts_document(facts)["binary_path"] == (
            collector.BINARY_PATH_PLACEHOLDER
        )
        assert str(tmp_path) not in document
        assert facts.digest in document

    def test_hung_version_probe_fails_within_the_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hung = tmp_path / "hung-codex"
        hung.write_text("#!/bin/sh\nsleep 5\n")
        hung.chmod(hung.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setattr(collector, "VERSION_PROBE_TIMEOUT_SECONDS", 0.5)
        with pytest.raises(collector.QualificationError, match="did not answer within"):
            collector.resolve_release_facts(str(hung))

    def test_refuses_output_without_a_codex_cli_release(self, tmp_path: Path) -> None:
        binary = tmp_path / "mystery"
        binary.write_text("#!/bin/sh\necho 'some other tool 1.2.3'\n")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        with pytest.raises(collector.QualificationError, match="no codex-cli release"):
            collector.resolve_release_facts(str(binary))


class TestGenerateSchema:
    def test_runs_the_documented_command_and_normalizes_in_place(self, tmp_path: Path) -> None:
        binary = write_fake_codex(tmp_path)
        out_dir = tmp_path / "schema-out"
        collector.generate_schema(str(binary), out_dir)
        assert sorted(path.name for path in out_dir.iterdir()) == sorted(SCHEMA_FILE_NAMES)
        normalized = json.loads((out_dir / "ClientRequest.json").read_text())
        assert normalized == {"properties": {"ant": {}, "zebra": {}}, "type": "object"}
        raw = (out_dir / "ClientRequest.json").read_text()
        assert raw == collector.normalize_json(normalized)
        assert "zebra" not in raw[: raw.index("ant")]

    def test_refuses_when_a_required_file_is_missing(self, tmp_path: Path) -> None:
        script = tmp_path / "partial-codex"
        script.write_text(
            f"""#!{sys.executable}
import sys
from pathlib import Path
if "--out" not in sys.argv:
    raise SystemExit(2)
out = Path(sys.argv[sys.argv.index("--out") + 1])
out.mkdir(parents=True, exist_ok=True)
(out / "ClientRequest.json").write_text("{{}}")
"""
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        with pytest.raises(collector.QualificationError, match="omitted required files"):
            collector.generate_schema(str(script), tmp_path / "partial-out")


class TestCollectEvidence:
    def test_end_to_end_with_fake_binary_and_explicit_baseline(self, tmp_path: Path) -> None:
        binary = write_fake_codex(tmp_path)
        prior = tmp_path / "prior"
        (prior / "protocol_schema").mkdir(parents=True)
        (prior / "installed_release.json").write_text(
            collector.normalize_json({"installed_version": "codex-cli 0.0.0-test"})
        )
        (prior / "protocol_schema" / "ClientRequest.json").write_text('{"type": "object"}')
        (prior / "protocol_schema" / "Retired.json").write_text("{}")

        out = tmp_path / "qualify" / "0.0.0-test"
        exit_code = collector.collect_evidence(out, codex_binary=str(binary), baseline=prior)

        assert exit_code == 0
        facts = json.loads((out / "installed_release.json").read_text())
        assert facts["installed_version"] == "codex-cli 0.0.0-test"
        assert len(facts["binary_sha256"]) == 64
        assert (out / "protocol_schema" / "ServerRequest.json").is_file()
        # Re-running into the finished bundle must refuse: prior evidence is
        # never overwritten in place.
        with pytest.raises(collector.QualificationError, match="never overwritten"):
            collector.collect_evidence(out, codex_binary=str(binary), baseline=prior)


class TestDefaultBaselineValidation:
    """default_baseline itself must fail closed on malicious index shapes."""

    def test_default_baseline_selects_the_real_qualified_bundle(self) -> None:
        assert collector.default_baseline() == (collector.FIXTURE_ROOT / "0.154.0")

    @staticmethod
    def _write_mini_bundle(root: Path, directory: str, *, complete: bool = True) -> None:
        bundle = root / directory
        (bundle / "protocol_schema").mkdir(parents=True)
        files = list(collector.BEHAVIOR_FILES)
        if not complete:
            files = files[:-1]
        for name in files:
            (bundle / name).write_text("{}")
        for name in collector.SCHEMA_FILES:
            (bundle / "protocol_schema" / name).write_text("{}")

    @pytest.fixture()
    def fixtures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "fixtures"
        root.mkdir()
        monkeypatch.setattr(collector, "FIXTURE_ROOT", root)
        return root

    @staticmethod
    def _write_index(root: Path, bundles: dict) -> None:
        (root / "index.json").write_text(json.dumps({"bundles": bundles}))

    def _qualified_entry(self, directory: str = "0.154.0") -> dict:
        return {"directory": directory, "status": "qualified", "compatibility_policy": "any"}

    def test_selects_the_unique_qualified_bundle(self, fixtures: Path) -> None:
        self._write_mini_bundle(fixtures, "0.154.0")
        self._write_index(fixtures, {"0.154.0": self._qualified_entry()})
        assert collector.default_baseline() == fixtures / "0.154.0"

    @pytest.mark.parametrize("directory", ["../secrets", "/tmp/escape", "0.154.0/.."])
    def test_refuses_escaping_or_absolute_directories(self, fixtures: Path, directory: str) -> None:
        self._write_mini_bundle(fixtures, "0.154.0")
        entry = self._qualified_entry(directory=directory)
        self._write_index(fixtures, {"0.154.0": entry})
        with pytest.raises(collector.QualificationError, match="malformed"):
            collector.default_baseline()

    def test_refuses_duplicate_directories(self, fixtures: Path) -> None:
        self._write_mini_bundle(fixtures, "0.154.0")
        self._write_index(
            fixtures,
            {"0.154.0": self._qualified_entry(), "0.154.1": self._qualified_entry()},
        )
        with pytest.raises(collector.QualificationError, match="claimed by both"):
            collector.default_baseline()

    def test_refuses_an_invalid_status(self, fixtures: Path) -> None:
        self._write_mini_bundle(fixtures, "0.154.0")
        entry = self._qualified_entry() | {"status": "provisional"}
        self._write_index(fixtures, {"0.154.0": entry})
        with pytest.raises(collector.QualificationError, match="invalid status"):
            collector.default_baseline()

    def test_refuses_an_incomplete_qualified_bundle(self, fixtures: Path) -> None:
        self._write_mini_bundle(fixtures, "0.154.0", complete=False)
        self._write_index(fixtures, {"0.154.0": self._qualified_entry()})
        with pytest.raises(collector.QualificationError, match="incomplete"):
            collector.default_baseline()

    def test_refuses_a_qualified_bundle_under_candidates(self, fixtures: Path) -> None:
        self._write_mini_bundle(fixtures, "candidates/0.154.0")
        entry = self._qualified_entry(directory="candidates/0.154.0")
        self._write_index(fixtures, {"0.154.0": entry})
        with pytest.raises(collector.QualificationError, match="fixture root"):
            collector.default_baseline()

    def test_refuses_a_version_directory_mismatch(self, fixtures: Path) -> None:
        self._write_mini_bundle(fixtures, "0.154.0")
        entry = self._qualified_entry(directory="0.154.0")
        self._write_index(fixtures, {"0.9.9": entry})
        with pytest.raises(collector.QualificationError, match="fixture root"):
            collector.default_baseline()

    def test_refuses_a_candidate_at_release_root(self, fixtures: Path) -> None:
        self._write_mini_bundle(fixtures, "0.155.0")
        entry = {"directory": "0.155.0", "status": "candidate"}
        self._write_index(fixtures, {"0.155.0": entry})
        with pytest.raises(collector.QualificationError, match="candidates/"):
            collector.default_baseline()

    def test_refuses_zero_or_multiple_qualified_bundles(self, fixtures: Path) -> None:
        self._write_mini_bundle(fixtures, "0.154.0")
        self._write_mini_bundle(fixtures, "0.155.0")
        self._write_index(
            fixtures,
            {"0.154.0": self._qualified_entry(), "0.155.0": self._qualified_entry("0.155.0")},
        )
        with pytest.raises(collector.QualificationError, match=r"multiple \(or zero\) qualified"):
            collector.default_baseline()


class TestAllowlistIndependence:
    def test_collector_never_imports_theater_or_names_the_allowlist(self) -> None:
        source = Path(collector.__file__).read_text()
        assert "from theater" not in source
        assert "import theater" not in source
        assert "CODEX_RUNTIME_VERIFIED_VERSIONS" not in source


class FakeInitializeClient:
    """Stands in for NativeWebSocketClient; records every initialize."""

    calls = 0

    def __init__(self, socket_path: Path, user_agent: str = "") -> None:
        self.socket_path = socket_path
        self.user_agent = user_agent
        self.initialize_calls = 0

    def initialize(self, *, experimental: bool = False) -> dict:
        self.initialize_calls += 1
        type(self).calls += 1
        return {"result": {"userAgent": self.user_agent}}

    def close(self) -> None:
        pass


class _StubServer:
    """Duck-typed AppServerProcess: the offline test never starts a backend."""

    def terminate(self) -> None:
        pass

    def alive(self) -> bool:
        return False


class TestBackendVersionCheck:
    def _session(
        self, monkeypatch: pytest.MonkeyPatch, user_agent: str
    ) -> collector.CaptureSession:
        monkeypatch.setattr(
            collector, "NativeWebSocketClient", lambda sock: FakeInitializeClient(sock, user_agent)
        )
        return collector.CaptureSession(
            root=Path("/run-root"),
            repo=Path("/run-root/repo"),
            socket_path=Path("/run-root/control.sock"),
            app_server=cast(AppServerProcess, _StubServer()),
            codex_home=Path("/run-root/home"),
            roots={},
            binary="/resolved/codex",
        )

    def test_initializes_exactly_once_and_parses_the_backend_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeInitializeClient.calls = 0
        session = self._session(monkeypatch, "codex-cli codex_ai 0.154.0 (homebrew) on macOS")
        assert collector._capture_version(session) == "0.154.0"
        assert FakeInitializeClient.calls == 1
        assert session.initialize_results[-1]["userAgent"].startswith("codex-cli")

    def test_refuses_a_user_agent_without_a_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = self._session(monkeypatch, "unknown-agent")
        with pytest.raises(collector.QualificationError, match="no codex-cli version"):
            collector._capture_version(session)


class TestEvidenceThreadingAndExitCodes:
    def test_offline_path_threads_the_resolved_binary_into_schema_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = write_fake_codex(tmp_path)
        seen: dict[str, str] = {}

        def fake_generate_schema(codex_binary: str, out_dir: Path) -> None:
            seen["binary"] = codex_binary

        monkeypatch.setattr(collector, "generate_schema", fake_generate_schema)
        out = tmp_path / "out"
        exit_code = collector.collect_evidence(out, codex_binary=str(binary))
        assert exit_code == 0
        assert seen["binary"] == str(binary.resolve())

    @pytest.mark.parametrize(
        "raised",
        [
            subprocess.TimeoutExpired(cmd="codex", timeout=1),
            OSError("binary disappeared"),
            collector.QualificationError("refused"),
        ],
    )
    def test_main_reports_boundary_errors_as_exit_code_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: Exception
    ) -> None:
        def failing_collect_evidence(*args: object, **kwargs: object) -> int:
            raise raised

        monkeypatch.setattr(collector, "collect_evidence", failing_collect_evidence)
        assert collector.main(["--out", str(tmp_path / "bundle")]) == 2
