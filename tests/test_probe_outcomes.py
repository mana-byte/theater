"""Expected probe absence and cancellation stay distinct from operational errors."""

from __future__ import annotations

import asyncio
import subprocess

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from theater import proc, timing
from theater.daemon.worktrees.repository import _git
from theater.observability import tracing
from theater.observability.catalog import PROC_PS_COMM


@pytest.fixture
def spans(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_get_tracer", lambda: provider.get_tracer("probe-test"))
    try:
        yield exporter
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    "error,is_error",
    [
        (subprocess.CalledProcessError(1, "ps", output="", stderr=""), False),
        (subprocess.CalledProcessError(1, "ps", output="", stderr="permission denied"), True),
        (subprocess.TimeoutExpired("ps", 5), True),
    ],
)
def test_process_probe_only_treats_empty_absence_as_normal(monkeypatch, spans, error, is_error):
    def check_output(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(subprocess, "check_output", check_output)
    assert proc.comm(123) == ""
    (span,) = spans.get_finished_spans()
    assert (span.status.status_code is StatusCode.ERROR) is is_error


@pytest.mark.parametrize("returncode,expected", [(1, (0,)), (1, (0, 1)), (128, (0, 1))])
def test_git_probe_expected_codes_are_explicit(monkeypatch, spans, returncode, expected):
    monkeypatch.setattr(
        subprocess, "run", lambda args, **_kwargs: subprocess.CompletedProcess(args, returncode)
    )
    assert _git(["git", "show-ref"], expected_returncodes=expected).returncode == returncode
    (span,) = spans.get_finished_spans()
    assert (span.status.status_code is StatusCode.ERROR) is (returncode not in expected)


def test_cancelled_span_preserves_cancellation_without_error_status(spans):
    with pytest.raises(asyncio.CancelledError), timing.span(PROC_PS_COMM, pid=123):
        raise asyncio.CancelledError
    (span,) = spans.get_finished_spans()
    assert span.attributes["result"] == "cancelled"
    assert span.status.status_code is StatusCode.UNSET
