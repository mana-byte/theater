"""B03 raw-client smoke shape for a coordinator-supplied candidate daemon socket."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_CANDIDATE_SOCKET = os.environ.get("RC10_CANDIDATE_FRONTEND_SOCKET")
_RAW_PROCESS = """
import json
import sys

sys.path.insert(0, sys.argv[2])
from rc10_support.raw_client import RawClient

client = RawClient(sys.argv[1])
client.connect()
try:
    handshake = client.handshake({
        "api": {"major": 1, "minor": 0},
        "client_id": "rc10-raw-b03",
        "role": "operator",
        "channel": "rpc",
        "required_capabilities": ["contract.v1"],
    })
    contract = client.call("frontend.contract.get", {})
    operator_to_provider = client.call(
        "frontend.providers.heartbeat", {"provider_generation": 0, "report_revision": 0}
    )
    public_to_private = client.call("ping", {})
finally:
    client.close()

def refused_handshake(api, required_capabilities):
    refused = RawClient(sys.argv[1])
    refused.connect()
    try:
        return refused.handshake({
            "api": api,
            "client_id": "rc10-raw-b03-refusal",
            "role": "operator",
            "channel": "rpc",
            "required_capabilities": required_capabilities,
        })
    finally:
        refused.close()

incompatible_api = refused_handshake({"major": 2, "minor": 0}, [])
missing_capability = refused_handshake(
    {"major": 1, "minor": 0}, ["rc10-b03-unavailable-capability"]
)
print(json.dumps({
    "handshake": handshake,
    "contract": contract,
    "operator_to_provider": operator_to_provider,
    "public_to_private": public_to_private,
    "incompatible_api": incompatible_api,
    "missing_capability": missing_capability,
}))
"""


@pytest.mark.skipif(
    _CANDIDATE_SOCKET is None,
    reason=(
        "coordinator wiring required: set RC10_CANDIDATE_FRONTEND_SOCKET to an already-running "
        "isolated S03 candidate public socket; this test never starts a daemon"
    ),
)
def test_raw_client_handshakes_and_reads_contract_from_candidate_daemon() -> None:
    """Exercise only public bytes from an independently imported stdlib child."""
    assert _CANDIDATE_SOCKET is not None
    completed = subprocess.run(
        [sys.executable, "-c", _RAW_PROCESS, _CANDIDATE_SOCKET, str(Path(__file__).parent)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    handshake = result["handshake"]
    contract = result["contract"]
    assert handshake["ok"] is True
    assert handshake["result"]["api"] == {"major": 1, "minor": 0}
    assert contract["ok"] is True
    assert isinstance(contract["result"], dict)
    assert result["operator_to_provider"]["error"]["code"] == "wrong_connection_role"
    assert result["public_to_private"]["error"]["code"] == "wrong_connection_role"
    assert result["incompatible_api"]["error"]["code"] == "incompatible_api"
    assert result["missing_capability"]["error"]["code"] == "missing_capability"
