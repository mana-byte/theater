"""Signed, participant-isolated Pi session roots."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

from theater import paths

from .constants import PI_ISOLATION_MARKER, PI_MARKER_VERSION


def canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _key_path() -> Path:
    return paths.marker_key_path("pi")


def _marker_key() -> bytes:
    path = _key_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(key)
        return key


def _marker_key_readonly() -> bytes | None:
    try:
        return _key_path().read_bytes()
    except FileNotFoundError:
        return None


def _mac(payload: dict[str, object], key: bytes) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def marker_text(*, participant_id: str, transcript_domain: Path) -> str:
    """Build the launch-owned marker for one Pi session directory."""
    payload: dict[str, object] = {
        "version": PI_MARKER_VERSION,
        "harness": "pi",
        "participant_id": participant_id,
        "transcript_domain": str(canonical(transcript_domain)),
        "domain_nonce": secrets.token_hex(16),
    }
    return json.dumps({**payload, "mac": _mac(payload, _marker_key())}, sort_keys=True) + "\n"


def validate_domain(
    transcript_domain: Path, *, participant_id: str | None = None
) -> dict[str, object] | None:
    """Accept only a same-user, signed Pi session directory."""
    domain = canonical(transcript_domain)
    marker = domain / PI_ISOLATION_MARKER
    try:
        domain_stat = domain.lstat()
        marker_stat = marker.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(domain_stat.st_mode) or not stat.S_ISREG(marker_stat.st_mode):
        return None
    if domain.is_symlink() or marker.is_symlink():
        return None
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    if euid is not None and (domain_stat.st_uid != euid or marker_stat.st_uid != euid):
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    received_mac = data.pop("mac", None)
    key = _marker_key_readonly()
    if key is None or not isinstance(received_mac, str):
        return None
    if not hmac.compare_digest(received_mac, _mac(data, key)):
        return None
    if data.get("version") != PI_MARKER_VERSION or data.get("harness") != "pi":
        return None
    if data.get("transcript_domain") != str(domain):
        return None
    if participant_id is not None and data.get("participant_id") != participant_id:
        return None
    if not isinstance(data.get("domain_nonce"), str):
        return None
    return data
