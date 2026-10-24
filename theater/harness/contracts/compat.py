"""Optional qualification declarations: how to run one stock CLI hermetically against a mock."""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Literal, Protocol

from theater.harness.contracts.values import freeze_json_mapping

#: Wire protocols the qualification mock speaks; ``openai-chat`` is Chat Completions.
COMPAT_PROTOCOLS = ("anthropic", "openai-chat", "openai-responses")

type CompatProtocol = Literal["anthropic", "openai-chat", "openai-responses"]


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
class ToolTemplate:
    """A scripted tool call: the CLI's tool name and a payload with substitution tokens."""

    name: str
    #: JSON object arguments, or freeform text for grammar-based tools such as a patch.
    payload: str | Mapping[str, object]
    #: The only tokens the payload may carry; everything else is literal.
    TOKENS: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        if isinstance(self.payload, Mapping):
            object.__setattr__(self, "payload", freeze_json_mapping(self.payload))

    @property
    def placeholders(self) -> frozenset[str]:
        """The token names the payload uses."""
        found: set[str] = set()
        _scan(self.payload, _pattern(self.TOKENS), found)
        return frozenset(found)

    @property
    def size(self) -> int:
        """Characters of the payload with its tokens unexpanded, JSON-encoded when structured."""
        rendered = self._render({token: "{" + token + "}" for token in self.TOKENS})
        return len(rendered if isinstance(rendered, str) else json.dumps(rendered))

    def _render(self, values: Mapping[str, str]) -> str | Mapping[str, object]:
        """Substitute the tokens in one pass; no other interpolation happens."""
        rendered = _substitute(self.payload, _pattern(self.TOKENS), values)
        assert isinstance(rendered, (str, Mapping))
        return rendered


@dataclass(frozen=True, slots=True)
class NativeEdit(ToolTemplate):
    """The CLI's own file-write tool, and a payload that makes it write one scratch file."""

    TOKENS: ClassVar[tuple[str, ...]] = ("path", "nonce")

    def render(self, *, path: str, nonce: str) -> str | Mapping[str, object]:
        return self._render({"path": path, "nonce": nonce})


@dataclass(frozen=True, slots=True)
class McpCall(ToolTemplate):
    """How the CLI's model calls an MCP tool when the tool is not offered under its own name.

    Needed only where MCP tools sit behind another tool (a script runner or a tool search);
    otherwise the suite finds the tool among the names the model was offered.
    """

    TOKENS: ClassVar[tuple[str, ...]] = ("server", "tool", "arguments")

    def render(
        self, *, server: str, tool: str, arguments: Mapping[str, object]
    ) -> str | Mapping[str, object]:
        """``arguments`` is substituted as its JSON text."""
        return self._render({"server": server, "tool": tool, "arguments": json.dumps(arguments)})


@functools.cache
def _pattern(tokens: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"\{(" + "|".join(map(re.escape, tokens)) + r")\}")


def _scan(value: object, pattern: re.Pattern[str], found: set[str]) -> None:
    if isinstance(value, str):
        found.update(pattern.findall(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            _scan(item, pattern, found)
    elif isinstance(value, tuple):
        for item in value:
            _scan(item, pattern, found)


def _substitute(value: object, pattern: re.Pattern[str], values: Mapping[str, str]) -> object:
    if isinstance(value, str):
        return pattern.sub(lambda match: values[match.group(1)], value)
    if isinstance(value, Mapping):
        return {key: _substitute(item, pattern, values) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_substitute(item, pattern, values) for item in value)
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
    #: Only when the model cannot call an MCP tool under its own name.
    mcp_call: McpCall | None = None
    #: Extra generic mutations whose effects this plugin's provisioning cannot mask.
    canaries: tuple[str, ...] = ()
    #: Non-model HTTP routes the mock acknowledges with 204, without consuming a reply.
    acknowledged_routes: tuple[tuple[str, str], ...] = ()


__all__ = [
    "COMPAT_PROTOCOLS",
    "CompatManifest",
    "CompatProtocol",
    "McpCall",
    "NativeEdit",
    "ProvisionContext",
    "Provisioned",
    "Provisioner",
    "ToolTemplate",
]
