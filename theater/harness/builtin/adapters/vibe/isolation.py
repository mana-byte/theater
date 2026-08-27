"""Signed Vibe transcript-domain markers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

from theater import paths

from .constants import _MARKER_KEY, _MARKER_VERSION, ISOLATION_MARKER


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _marker_key() -> bytes:
    """Read or create the daemon-local Vibe domain signing key."""
    key_path = paths.home() / _MARKER_KEY
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return key_path.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key


def _marker_key_readonly() -> bytes | None:
    """Read the signing key without creating it during validation."""
    key_path = paths.home() / _MARKER_KEY
    try:
        return key_path.read_bytes()
    except FileNotFoundError:
        return None


def _marker_mac(payload: dict[str, object]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(_marker_key(), body, hashlib.sha256).hexdigest()


def _marker_mac_readonly(payload: dict[str, object]) -> str | None:
    """Verify a MAC without creating the key. Returns None if no key exists."""
    key = _marker_key_readonly()
    if key is None:
        return None
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def isolation_marker_text(*, participant_id: str, transcript_domain: Path) -> str:
    """Signed marker content for the participant that created a Vibe root."""
    payload: dict[str, object] = {
        "version": _MARKER_VERSION,
        "harness": "vibe",
        "participant_id": participant_id,
        "transcript_domain": str(_canonical(transcript_domain)),
        "domain_nonce": secrets.token_hex(16),
    }
    return json.dumps({**payload, "mac": _marker_mac(payload)}, sort_keys=True) + "\n"


def validate_isolated_domain(
    transcript_domain: Path, *, participant_id: str | None = None
) -> dict[str, object] | None:
    """Validate a signed same-owner domain; trusted lineage is checked separately."""
    domain = _canonical(transcript_domain)
    marker = domain / ISOLATION_MARKER
    try:
        domain_st = domain.lstat()
        marker_st = marker.lstat()
    except OSError:
        return None
    if not domain.is_dir() or not stat.S_ISDIR(domain_st.st_mode):
        return None
    if domain.is_symlink() or marker.is_symlink() or not marker.is_file():
        return None
    if not stat.S_ISREG(marker_st.st_mode):
        return None
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    if euid is not None and (domain_st.st_uid != euid or marker_st.st_uid != euid):
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    mac = data.pop("mac", None)
    expected_mac = _marker_mac_readonly(data)
    if (
        expected_mac is None
        or not isinstance(mac, str)
        or not hmac.compare_digest(mac, expected_mac)
    ):
        return None
    if data.get("version") != _MARKER_VERSION or data.get("harness") != "vibe":
        return None
    if data.get("transcript_domain") != str(domain):
        return None
    if participant_id is not None and data.get("participant_id") != participant_id:
        return None
    if not isinstance(data.get("domain_nonce"), str):
        return None
    return data
