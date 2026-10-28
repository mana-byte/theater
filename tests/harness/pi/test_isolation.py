"""Pi isolation markers accept only a same-user signed session domain."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from theater import paths
from theater.harness.builtin.plugins.pi import isolation
from theater.harness.builtin.plugins.pi.constants import PI_ISOLATION_MARKER
from theater.harness.builtin.plugins.pi.isolation import marker_text, validate_domain


def _signed_domain(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    domain = tmp_path / "sessions" / "pi-child"
    domain.mkdir(parents=True)
    marker = domain / PI_ISOLATION_MARKER
    marker.write_text(
        marker_text(participant_id="pi-child", transcript_domain=domain), encoding="utf-8"
    )
    return domain, marker


def _payload(marker: Path) -> dict[str, object]:
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_pi_validate_domain_accepts_its_own_signed_marker(tmp_path, monkeypatch) -> None:
    domain, marker = _signed_domain(tmp_path, monkeypatch)
    payload = _payload(marker)
    payload.pop("mac")
    assert validate_domain(domain, participant_id="pi-child") == payload
    assert validate_domain(domain) is not None


@pytest.mark.parametrize(
    ("changes", "resign"),
    [
        # A tampered MAC fails the digest; a correctly resigned marker with a
        # tampered field proves each post-MAC check refuses on its own.
        ({"mac": "0" * 64}, False),
        ({"mac": 123}, False),
        ({"version": 99}, True),
        ({"harness": "claude"}, True),
        ({"transcript_domain": "other"}, True),
        ({"participant_id": "other"}, True),
        ({"domain_nonce": 1}, True),
    ],
)
def test_pi_validate_domain_refuses_forged_markers(
    tmp_path, monkeypatch, changes: dict[str, object], resign: bool
) -> None:
    domain, marker = _signed_domain(tmp_path, monkeypatch)
    payload = _payload(marker)
    if resign:
        payload.pop("mac")
        payload.update(changes)
        payload["mac"] = isolation._mac(payload, isolation._marker_key())
    else:
        payload.update(changes)
    marker.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    assert validate_domain(domain, participant_id="pi-child") is None


@pytest.mark.parametrize("content", ["not json", "[]"])
def test_pi_validate_domain_refuses_an_unparseable_marker(
    tmp_path, monkeypatch, content: str
) -> None:
    domain, marker = _signed_domain(tmp_path, monkeypatch)
    marker.write_text(content, encoding="utf-8")
    assert validate_domain(domain, participant_id="pi-child") is None


def test_pi_validate_domain_refuses_a_missing_marker(tmp_path, monkeypatch) -> None:
    domain, marker = _signed_domain(tmp_path, monkeypatch)
    marker.unlink()
    assert validate_domain(domain, participant_id="pi-child") is None


def test_pi_validate_domain_refuses_a_non_regular_marker(tmp_path, monkeypatch) -> None:
    domain, marker = _signed_domain(tmp_path, monkeypatch)
    marker.unlink()
    marker.mkdir()
    assert validate_domain(domain, participant_id="pi-child") is None


def test_pi_validate_domain_refuses_without_a_marker_key(tmp_path, monkeypatch) -> None:
    domain, _marker = _signed_domain(tmp_path, monkeypatch)
    paths.marker_key_path("pi").unlink()
    assert validate_domain(domain, participant_id="pi-child") is None


def test_pi_validate_domain_refuses_a_marker_that_became_a_symlink(tmp_path, monkeypatch) -> None:
    # Race coverage: the marker swapped to a symlink between lstat and recheck.
    domain, marker = _signed_domain(tmp_path, monkeypatch)
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == marker or real_is_symlink(self))
    assert validate_domain(domain, participant_id="pi-child") is None


def test_pi_validate_domain_refuses_a_foreign_owner(tmp_path, monkeypatch) -> None:
    domain, _marker = _signed_domain(tmp_path, monkeypatch)
    real_euid = os.geteuid()
    monkeypatch.setattr(isolation.os, "geteuid", lambda: real_euid + 1)
    assert validate_domain(domain, participant_id="pi-child") is None
