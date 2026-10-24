"""Optional qualification declarations: how to run one stock CLI hermetically against a mock."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from theater.harness.contracts.values import freeze_json_mapping

#: Wire protocols the qualification mock speaks; ``openai`` is Chat Completions.
COMPAT_PROTOCOLS = ("anthropic", "openai", "openai-responses")

type CompatProtocol = Literal["anthropic", "openai", "openai-responses"]

#: The only tokens a native-edit payload may carry; everything else is literal.
_PLACEHOLDER = re.compile(r"\{(path|nonce)\}")


@dataclass(frozen=True, slots=True)
class ProvisionContext:
    """Where one hermetic qualification run lives and what the mock model serves."""

    sandbox_root: Path
    #: Becomes the stock CLI's HOME.
    home: Path
    config_dir: Path
    workdir: Path
    #: Base URL per protocol: the bare origin for ``anthropic``, ending in ``/v1`` for OpenAI.
    mock_urls: Mapping[str, str]
    binary: Path
    version: str
    #: Logical model names the run will request; provisioning makes the CLI accept them.
    models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mock_urls", MappingProxyType(dict(self.mock_urls)))
        object.__setattr__(self, "models", tuple(self.models))


@dataclass(frozen=True, slots=True)
class Provisioned:
    """What makes the stock CLI talk to the mock: nothing else may change its launch."""

    protocol: CompatProtocol
    #: Additions to the CLI environment: endpoint, dummy credential, update/telemetry opt-outs.
    env: Mapping[str, str] = MappingProxyType({})
    #: Absolute config, catalog and onboarding or trust pre-seeding files.
    files: Mapping[Path, str] = MappingProxyType({})
    #: Selectors the CLI accepts for the logical models, routed to the mock.
    models: tuple[str, ...] = ()
    #: Harness-native effort values worth sampling.
    efforts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.protocol not in COMPAT_PROTOCOLS:
            raise ValueError(f"protocol must be one of {', '.join(COMPAT_PROTOCOLS)}")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))
        object.__setattr__(self, "models", tuple(self.models))
        object.__setattr__(self, "efforts", tuple(self.efforts))


@dataclass(frozen=True, slots=True)
class NativeEdit:
    """The CLI's own file-write tool, and a payload that makes it write one scratch file."""

    name: str
    #: JSON object arguments, or freeform text for grammar-based tools such as a patch.
    payload: str | Mapping[str, object]

    def __post_init__(self) -> None:
        if isinstance(self.payload, Mapping):
            object.__setattr__(self, "payload", freeze_json_mapping(self.payload))

    @property
    def placeholders(self) -> frozenset[str]:
        """The placeholder names the payload uses (``path`` and ``nonce`` only)."""
        found: set[str] = set()
        _scan(self.payload, found)
        return frozenset(found)

    def render(self, *, path: str, nonce: str) -> str | Mapping[str, object]:
        """Substitute the placeholders in one pass; no other interpolation happens."""
        rendered = _substitute(self.payload, {"path": path, "nonce": nonce})
        assert isinstance(rendered, (str, Mapping))
        return rendered


def _scan(value: object, found: set[str]) -> None:
    if isinstance(value, str):
        found.update(_PLACEHOLDER.findall(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            _scan(item, found)
    elif isinstance(value, tuple):
        for item in value:
            _scan(item, found)


def _substitute(value: object, values: Mapping[str, str]) -> object:
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda match: values[match.group(1)], value)
    if isinstance(value, Mapping):
        return {key: _substitute(item, values) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_substitute(item, values) for item in value)
    return value


class Provisioner(Protocol):
    """Pure: returns the env and files that point one stock CLI at the mock."""

    def __call__(self, context: ProvisionContext) -> Provisioned: ...


@dataclass(frozen=True, slots=True)
class CompatManifest:
    """How a qualification run provisions this harness; every scenario is derived elsewhere.

    ``provision`` may set an endpoint, a dummy credential, a model catalog and onboarding or
    trust state. It must not replace launch argv, approval policy or receipt commands.
    """

    provision: Provisioner
    #: Only for harnesses declaring the ``edits`` approval.
    native_edit: NativeEdit | None = None


__all__ = [
    "COMPAT_PROTOCOLS",
    "CompatManifest",
    "CompatProtocol",
    "NativeEdit",
    "ProvisionContext",
    "Provisioned",
    "Provisioner",
]
