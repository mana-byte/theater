"""W3C trace context over NDJSON and daemon SERVER RPC dispatch spans."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest

from theater import protocol
from theater.daemon.runtime.socket import dispatch, handle_connection

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


@pytest.mark.parametrize("payload", [5, [], "hi", None, True])
async def test_dispatch_json_primitive_is_a_bad_request_with_id_zero(payload):
    response = await dispatch(SimpleNamespace(), json.dumps(payload).encode(), methods={})

    decoded = json.loads(response)
    assert decoded["id"] == 0
    assert decoded["error"]["code"] == "bad_request"


@pytest.mark.parametrize(
    "payload",
    [
        {"id": 7, "method": 42},
        {"id": 7, "method": "ping", "params": None},
    ],
)
async def test_dispatch_rejects_invalid_request_fields_with_the_usable_id(payload):
    response = await dispatch(SimpleNamespace(), json.dumps(payload).encode(), methods={})

    decoded = json.loads(response)
    assert decoded["id"] == 7
    assert decoded["error"]["code"] == "bad_request"


@pytest.mark.parametrize("req_id", [True, "7", 7.0, None])
async def test_dispatch_uses_id_zero_for_non_integer_ids(req_id):
    async def _ping(_daemon, _params):
        return True

    response = await dispatch(
        SimpleNamespace(),
        json.dumps({"id": req_id, "method": "ping"}).encode(),
        methods={"ping": _ping},
    )

    decoded = json.loads(response)
    assert decoded["id"] == 0
    assert decoded["ok"] is True


async def test_dispatch_defaults_absent_params_to_an_object():
    seen = []

    async def _ping(daemon, params):
        seen.append(params)
        return True

    response = await dispatch(
        SimpleNamespace(), b'{"id":1,"method":"ping"}', methods={"ping": _ping}
    )

    assert json.loads(response)["ok"] is True
    assert seen == [{}]


async def test_malformed_request_does_not_close_the_connection():
    class Writer:
        def __init__(self):
            self.responses = []

        def write(self, response):
            self.responses.append(response)

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    reader = asyncio.StreamReader()
    reader.feed_data(b"5\n" + protocol.request(1, "ping"))
    reader.feed_eof()
    writer = Writer()
    daemon = SimpleNamespace(_conns=set())

    async def _ping(_daemon, _params):
        return True

    async def _dispatch(line):
        return await dispatch(
            daemon,
            line,
            methods={"ping": _ping},
        )

    daemon._dispatch = _dispatch
    await handle_connection(daemon, reader, writer)

    assert [json.loads(response)["error"]["code"] for response in writer.responses[:1]] == [
        "bad_request"
    ]
    assert json.loads(writer.responses[1])["result"] is True


async def test_dispatch_invalid_request_creates_no_timing_span(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    await dispatch(SimpleNamespace(), b"not json\n", methods={})
    await dispatch(SimpleNamespace(), b"5\n", methods={})
    await dispatch(SimpleNamespace(), b'{"id":1,"method":42}', methods={})
    await dispatch(SimpleNamespace(), b'{"id":1,"method":"ping","params":null}', methods={})
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
        "from theater.observability.catalog import RPC_CLIENT\n"
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
        "    with span(RPC_CLIENT,method='ping'):\n"
        "        m=inject_trace_context()\n"
        "        raw=protocol.request(1,'ping',meta=m or None)\n"
        "    ctx=extract_trace_context(json.loads(raw).get('_meta'))\n"
        "    assert ctx is not None,'_meta must carry valid W3C context'\n"
        "    await dispatch(d,raw,methods={'ping':_ping})\n"
        "    spans=[s for s in e.spans if 'rpc' in s.name]\n"
        "    assert len(spans)>=2\n"
        "    c=[s for s in spans if s.name=='rpc.client ping'][0]\n"
        "    sv=[s for s in spans if s.name=='rpc.server ping'][0]\n"
        "    assert c.context.trace_id==sv.context.trace_id\n"
        "    assert sv.parent is not None\n"
        "    assert sv.parent.span_id==c.context.span_id\n"
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
