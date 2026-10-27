"""The stayed-uncertain send WARNING keeps its receipt fields through CI excerpt clipping."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.support.control_harness import Harness, make_runtime, wrap_runtime
from tests.support.fake_runtime import FakeRuntime
from tests.support.log_excerpt import log_excerpt
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    ControlReceipt,
    DeliveryResult,
    SessionOpenMode,
)
from theater.observability.logging import make_formatter

PARTICIPANT = "c5c80be8d5b2"
SESSION = "6f9a2c0e-8d34-4b1a-9f7c-2e5d8b3a1c4d"


async def test_unknown_send_warning_survives_log_excerpt_clipping(
    store: Store, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A clipped CI excerpt still names the operation, receipt code and native session."""

    sent: list[str] = []

    class UnconfirmedSend(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            sent.append(operation_id)
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.UNKNOWN,
                error_code="send_unconfirmed",
                error="the send entry did not appear on the session tree within the window",
            )

    runtime = wrap_runtime(make_runtime(PARTICIPANT), UnconfirmedSend)
    harness = Harness(store, {PARTICIPANT: runtime})
    await harness.runtimes[PARTICIPANT].open_session(
        mode=SessionOpenMode.RECONNECT, native_session_id=SESSION
    )

    with caplog.at_level(logging.WARNING, logger="theater.daemon.controls"):
        await harness.service.send(PARTICIPANT, caller_id="caller", prompt="diagnose it")

    record = next(r for r in caplog.records if "stayed uncertain" in r.getMessage())
    evidence = tmp_path / "regie"
    evidence.mkdir()
    (evidence / "daemon-start.log").write_text(
        f"{make_formatter().format(record)}\n", encoding="utf-8"
    )

    excerpt = log_excerpt(evidence)
    assert sent[0] in excerpt
    assert "send_unconfirmed" in excerpt
    assert SESSION in excerpt
