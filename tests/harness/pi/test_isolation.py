"""Pi isolation markers accept only a same-user signed session domain."""

from __future__ import annotations

import json
from collections.abc import Callable
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


def _reissue(marker: Path, payload: dict[str, object]) -> None:
    marker.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _forge(marker: Path, changes: dict[str, object]) -> None:
    # The post-mac checks are only reachable through a correctly signed marker.
    payload = _payload(marker)
    payload.pop("mac")
    payload.update(changes)
    payload["mac"] = isolation._mac(payload, isolation._marker_key())
    _reissue(marker, payload)


def test_pi_validate_domain_accepts_its_own_signed_marker(tmp_path, monkeypatch) -> None:
    domain, marker = _signed_domain(tmp_path, monkeypatch)
    payload = _payload(marker)
    payload.pop("mac")
    assert validate_domain(domain, participant_id="pi-child") == payload


def test_pi_validate_domain_defaults_to_the_marker_owner(tmp_path, monkeypatch) -> None:
    domain, marker = _signed_domain(tmp_path, monkeypatch)
    _forge(marker, {"participant_id": "other"})
    assert validate_domain(domain) is not None
    assert validate_domain(domain, participant_id="pi-child") is None


# Each damage case drives exactly one refusal branch of validate_domain.
Damage = Callable[[Path, Path, Path], None]


def _damage_missing_domain(tmp_path: Path, domain: Path, marker: Path) -> None:
    del marker
    domain.rename(tmp_path / "gone")


def _damage_missing_marker(tmp_path: Path, domain: Path, marker: Path) -> None:
    del tmp_path, domain
    marker.unlink()


def _damage_marker_not_regular(tmp_path: Path, domain: Path, marker: Path) -> None:
    del tmp_path, domain
    marker.unlink()
    marker.mkdir()


def _damage_domain_symlink(tmp_path: Path, domain: Path, marker: Path) -> None:
    del marker
    real = tmp_path / "real"
    domain.rename(real)
    domain.symlink_to(real)


def _damage_marker_symlink(tmp_path: Path, domain: Path, marker: Path) -> None:
    del domain
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    marker.unlink()
    marker.symlink_to(target)


def _damage_marker_bad_json(tmp_path: Path, domain: Path, marker: Path) -> None:
    del tmp_path, domain
    marker.write_text("not json", encoding="utf-8")


def _damage_marker_not_an_object(tmp_path: Path, domain: Path, marker: Path) -> None:
    del tmp_path, domain
    marker.write_text("[]", encoding="utf-8")


def _damage_forged_mac(tmp_path: Path, domain: Path, marker: Path) -> None:
    del tmp_path, domain
    payload = _payload(marker)
    payload["mac"] = "0" * 64
    _reissue(marker, payload)


def _damage_mac_not_a_string(tmp_path: Path, domain: Path, marker: Path) -> None:
    del tmp_path, domain
    payload = _payload(marker)
    payload["mac"] = 123
    _reissue(marker, payload)


def _damage_no_marker_key(tmp_path: Path, domain: Path, marker: Path) -> None:
    del tmp_path, domain, marker
    paths.marker_key_path("pi").unlink()


def _damage_wrong_version(tmp_path: Path, domain: Path, marker: Path) -> None:
    del tmp_path, domain
    _forge(marker, {"version": 99})


def _damage_wrong_harness(tmp_path: Path, domain: Path, marker: Path) -> None:
    del tmp_path, domain
    _forge(marker, {"harness": "claude"})


def _damage_wrong_domain(tmp_path: Path, domain: Path, marker: Path) -> None:
    del domain
    _forge(marker, {"transcript_domain": str(tmp_path)})


def _damage_wrong_owner(tmp_path: Path, domain: Path, marker: Path) -> None:
    del tmp_path, domain
    _forge(marker, {"participant_id": "other"})


def _damage_nonce_not_a_string(tmp_path: Path, domain: Path, marker: Path) -> None:
    del tmp_path, domain
    _forge(marker, {"domain_nonce": 1})


DAMAGES: dict[str, Damage] = {
    "missing-domain": _damage_missing_domain,
    "missing-marker": _damage_missing_marker,
    "marker-not-regular": _damage_marker_not_regular,
    "domain-symlink": _damage_domain_symlink,
    "marker-symlink": _damage_marker_symlink,
    "marker-bad-json": _damage_marker_bad_json,
    "marker-not-an-object": _damage_marker_not_an_object,
    "forged-mac": _damage_forged_mac,
    "mac-not-a-string": _damage_mac_not_a_string,
    "no-marker-key": _damage_no_marker_key,
    "wrong-version": _damage_wrong_version,
    "wrong-harness": _damage_wrong_harness,
    "wrong-domain": _damage_wrong_domain,
    "wrong-owner": _damage_wrong_owner,
    "nonce-not-a-string": _damage_nonce_not_a_string,
}


@pytest.mark.parametrize("damage", DAMAGES.values(), ids=DAMAGES.keys())
def test_pi_validate_domain_refuses_damaged_or_forged_markers(
    tmp_path, monkeypatch, damage: Damage
) -> None:
    domain, marker = _signed_domain(tmp_path, monkeypatch)
    damage(tmp_path, domain, marker)
    assert validate_domain(domain, participant_id="pi-child") is None
