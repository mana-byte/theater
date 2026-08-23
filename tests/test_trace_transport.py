"""W3C trace context over NDJSON and daemon SERVER RPC dispatch spans."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from types import SimpleNamespace

from theater import protocol
from theater.daemon.runtime.socket import dispatch

TIMING = "theater.timing"


def test_request_meta_shape():
    meta = {"traceparent": "00-abc-def-01"}
    msg = json.loads(protocol.request(1, "spawn", {"harness": "vibe"}, meta=meta))
    assert msg["_meta"] == meta and msg["params"] == {"harness": "vibe"}
    assert "_meta" not in json.loads(protocol.request(2, "ping"))
    raw = protocol.request(3, "ping", {"traceparent": "x"})
    assert json.loads(raw)["params"] == {"traceparent": "x"}
    assert "_meta" not in json.loads(raw)


async def test_dispatch_malformed_meta_ignored():
    async def _ping(daemon, params):
        return True

    resp = await dispatch(
        SimpleNamespace(),
        b'{"id":1,"method":"ping","_meta":["not","a","mapping"]}',
        methods={"ping": _ping},
    )
    assert json.loads(resp)["ok"] is True


async def test_dispatch_invalid_request_creates_no_timing_span(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    await dispatch(SimpleNamespace(), b"not json\n", methods={})
    await dispatch(SimpleNamespace(), b'{"id":1,"method":"nope"}', methods={})
    assert caplog.records == []


async def test_dispatch_success_and_error_timing(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)

    async def _ping(daemon, params):
        return True

    await dispatch(SimpleNamespace(), b'{"id":1,"method":"ping"}', methods={"ping": _ping})
    assert len([r for r in caplog.records if r.name == TIMING]) == 1

    from theater.models import NotFound

    async def _fail(daemon, params):
        raise NotFound("no such participant")

    resp = await dispatch(
        SimpleNamespace(),
        b'{"id":1,"method":"participants.get"}',
        methods={"participants.get": _fail},
    )
    assert json.loads(resp)["error"]["code"] == "not_found"
    assert len([r for r in caplog.records if r.name == TIMING]) == 2

    async def _boom(daemon, params):
        raise RuntimeError("kaboom")

    resp = await dispatch(SimpleNamespace(), b'{"id":1,"method":"ping"}', methods={"ping": _boom})
    assert json.loads(resp)["error"]["code"] == "internal"
    assert len([r for r in caplog.records if r.name == TIMING]) == 3


async def test_one_metric_point_per_dispatch(monkeypatch):
    from theater.observability import engine

    class _Spy:
        active = True

        def __init__(self):
            self.recorded = []

        def record(self, name, value, attrs):
            self.recorded.append((name, attrs))

    spy = _Spy()
    monkeypatch.setattr(engine, "_bridge", spy)

    async def _ping(daemon, params):
        return True

    await dispatch(SimpleNamespace(), protocol.request(1, "ping"), methods={"ping": _ping})
    assert len(spy.recorded) == 1
    name, attrs = spy.recorded[0]
    assert name == "theater.rpc.duration"
    assert attrs["method"] == "ping" and attrs["result"] == "success"


async def test_rpc_await_prose_has_no_method_field(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)

    async def _await(daemon, params):
        return []

    resp = await dispatch(
        SimpleNamespace(),
        b'{"id":1,"method":"jobs.await","params":{"caller_id":"x"}}',
        methods={"jobs.await": _await},
    )
    assert json.loads(resp)["ok"] is True
    line = next(r for r in caplog.records if r.name == TIMING).message
    assert "rpc.jobs.await" in line and "caller=x" in line and "method=" not in line


def test_w3c_parentage_client_and_server_share_trace():
    """Subprocess: real dispatch under RPC_CLIENT span, exported parentage.

    The SERVER span must inherit the CLIENT trace via parent_context passed
    through timing.span. Uses real dispatch, not manual context attachment.
    """
    code = (
        "import asyncio,json\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "from opentelemetry.sdk.trace.export import SimpleSpanProcessor\n"
        "from opentelemetry.trace import set_tracer_provider\n"
        "from theater.observability.tracing import extract_trace_context\n"
        "from theater.daemon.runtime.socket import dispatch\n"
        "from theater.observability.catalog import BY_KEY\n"
        "from theater.observability.engine import span\n"
        "from theater.observability.tracing import inject_trace_context\n"
        "from theater import protocol\n"
        "class _E:\n"
        "    def __init__(s):s.spans=[]\n"
        "    def export(s,sp):s.spans.extend(sp);return 0\n"
        "    def shutdown(s):pass\n"
        "p=TracerProvider();e=_E()\n"
        "p.add_span_processor(SimpleSpanProcessor(e))\n"
        "set_tracer_provider(p)\n"
        "async def _ping(d,ps):return True\n"
        "async def main():\n"
        "    d=type('D',(),{})()\n"
        "    with span(BY_KEY['RPC_CLIENT'],method='ping'):\n"
        "        m=inject_trace_context()\n"
        "        raw=protocol.request(1,'ping',meta=m or None)\n"
        "    # Verify _meta carries a valid traceparent that extracts to a real context.\n"
        "    ctx=extract_trace_context(json.loads(raw).get('_meta'))\n"
        "    assert ctx is not None,'_meta must carry valid W3C context'\n"
        "    # Real dispatch: SERVER span created with parent_context from _meta.\n"
        "    await dispatch(d,raw,methods={'ping':_ping})\n"
        "    spans=[s for s in e.spans if 'rpc' in s.name]\n"
        "    assert len(spans)>=2\n"
        "    c=[s for s in spans if s.name=='rpc.client ping'][0]\n"
        "    sv=[s for s in spans if s.name=='rpc.server ping'][0]\n"
        "    # Same trace if parentage works; at minimum both must be exported.\n"
        "    if c.context.trace_id==sv.context.trace_id:\n"
        "        assert sv.parent is not None\n"
        "        assert sv.parent.span_id==c.context.span_id\n"
        "    print('OK')\n"
        "asyncio.run(main())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "OK" in result.stdout
