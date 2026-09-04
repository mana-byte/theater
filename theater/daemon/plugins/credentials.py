"""Opaque credential minting and verifier comparison for MCP sidecars."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

from theater.constants.plugins import MCP_PLUGIN_CREDENTIAL_MAX_CHARS

_PREFIX = "theater-plugin-v1"


@dataclass(frozen=True, slots=True, repr=False)
class CredentialMaterial:
    """One launch-only secret plus the durable selector and verifier."""

    credential_id: str
    credential: str = field(repr=False)
    verifier: str = field(repr=False)


def mint_credential() -> CredentialMaterial:
    """Mint a high-entropy credential that reveals only a non-secret selector."""
    credential_id = secrets.token_urlsafe(12)
    secret = secrets.token_urlsafe(32)
    credential = f"{_PREFIX}.{credential_id}.{secret}"
    return CredentialMaterial(
        credential_id=credential_id,
        credential=credential,
        verifier=credential_verifier(credential),
    )


def credential_id_from_value(value: object) -> str | None:
    """Extract the public selector from a syntactically valid credential."""
    if not isinstance(value, str) or not value or len(value) > MCP_PLUGIN_CREDENTIAL_MAX_CHARS:
        return None
    prefix, separator, remainder = value.partition(".")
    if prefix != _PREFIX or not separator:
        return None
    credential_id, separator, secret = remainder.partition(".")
    if not separator or not credential_id or not secret:
        return None
    if not _url_token(credential_id) or not _url_token(secret):
        return None
    return credential_id


def credential_verifier(credential: str) -> str:
    """Return a non-reversible verifier for a high-entropy credential."""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def verifies(credential: object, verifier: object) -> bool:
    """Compare a supplied credential to a stored verifier without timing leaks."""
    if (
        not isinstance(credential, str)
        or credential_id_from_value(credential) is None
        or not isinstance(verifier, str)
    ):
        return False
    return hmac.compare_digest(credential_verifier(credential), verifier)


def _url_token(value: str) -> bool:
    return all(character.isalnum() or character in "-_" for character in value)


__all__ = [
    "CredentialMaterial",
    "credential_id_from_value",
    "credential_verifier",
    "mint_credential",
    "verifies",
]
