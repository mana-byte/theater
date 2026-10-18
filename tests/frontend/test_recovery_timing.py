"""State recovery remains explicit on the wire without becoming a server failure."""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from theater import paths
from theater.frontend import EventCursor, FrontendClient, FrontendResponseError
from theater.observability import tracing


@pytest.mark.parametrize(
    ("case", "code", "failed"),
    [
        ("follow", "resnapshot_required", False),
        ("expired_page", "snapshot_expired", False),
        ("bad_page", "bad_request", True),
    ],
)
async def test_recovery_response_keeps_wire_code_and_honest_span_status(
    daemon, monkeypatch, case, code, failed
):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_get_tracer", lambda: provider.get_tracer("test"))
    client = FrontendClient(paths.socket_path(), client_id="recovery-timing")
    try:
        snapshot_id = "expired"
        if case == "bad_page":
            snapshot_id = (await client.state.snapshot()).value.snapshot_id
        with pytest.raises(FrontendResponseError) as caught:
            if case == "follow":
                await client.state.follow(EventCursor("retired-stream", 0), wait_seconds=0)
            else:
                await client.state.page(snapshot_id, 999)
        assert caught.value.value.code == code
        method = "follow" if case == "follow" else "page"
        spans = [
            span
            for span in exporter.get_finished_spans()
            if span.name == f"rpc.server frontend.state.{method}"
        ]
        assert len(spans) == 1
        assert (spans[0].status.status_code is StatusCode.ERROR) is failed
        assert spans[0].attributes.get("theater.rpc.recovery") == (None if failed else code)
    finally:
        await client.close()
        provider.shutdown()
