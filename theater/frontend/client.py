"""Connect-only public SDK client with dedicated ordinary connection lanes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from jsonschema.exceptions import ValidationError

from theater.frontend.capabilities import (
    MAX_EXACT_JSON_INTEGER,
    MAX_FRAME_BYTES,
    METHOD_CATALOG,
    PUBLIC_API_MAJOR,
    PUBLIC_API_MINOR,
    ConnectionChannel,
    ConnectionRole,
    MethodSpec,
)
from theater.frontend.dto import HandshakeResult, Response
from theater.frontend.errors import FrontendError
from theater.frontend.schemas import (
    validate_public_request,
    validate_public_response,
    validator_for,
)
from theater.frontend.transport import (
    FrontendTransport,
    FrontendTransportError,
    RequestUncertain,
    TransportBusy,
    TransportProtocolError,
)

if TYPE_CHECKING:
    from theater.frontend._facades import (
        CatalogsClient,
        ContractClient,
        ControlsClient,
        DiagnosticsClient,
        HealthClient,
        JobsClient,
        OperationsClient,
        ParticipantsClient,
        ProvidersClient,
        RecallClient,
        SchemasClient,
        ScratchpadClient,
        SkillsClient,
        StateClient,
        TrajectoryClient,
        TranscriptsClient,
        UsageClient,
        WorkspacesClient,
    )


class ConnectionLane(StrEnum):
    """Independent public connections so long waits cannot block interactive work."""

    INTERACTIVE = "interactive"
    OPERATION_WAIT = "operation_wait"
    STATE_FOLLOW = "state_follow"
    PROVIDER_CALLBACK = "provider_callback"


class FrontendClientError(RuntimeError):
    """Base error raised before a public API refusal reaches the caller."""


class ClientStateError(FrontendClientError):
    """The client configuration or lifecycle does not permit this call."""


class MethodRoleError(FrontendClientError):
    """The configured connection role cannot call the selected catalog method."""


class CapabilityUnavailable(FrontendClientError):
    """The handshake did not negotiate a capability required by a method."""


class IdempotencyRequirementError(FrontendClientError):
    """A local call omitted or misused its catalog-required idempotency key."""


class ResponseCorrelationError(FrontendClientError):
    """The peer response cannot be correlated to this connection's one request."""


class ResponseValidationError(FrontendClientError):
    """A successful public response contradicted the bundled contract schemas."""


class NegotiationError(FrontendClientError):
    """A successful handshake did not negotiate the requested public API facts."""


class FrontendResponseError(FrontendError):
    """A refusal plus its full forward-compatible public response envelope."""

    def __init__(self, response: Response) -> None:
        assert response.error is not None
        super().__init__(response.error)
        self.response = response


class RequestTimedOut(RequestUncertain):
    """The local wait expired after a request started; its remote effect is unknown."""

    def __init__(self, method: str, request_id: int) -> None:
        FrontendTransportError.__init__(
            self,
            f"local wait for {method} with request id {request_id} timed out; "
            "the SDK closed only that lane and did not retry",
        )
        self.method = method
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class FrontendClientConfig:
    """Stable identity and explicit socket facts used for every client lane."""

    socket_path: str
    client_id: str
    role: ConnectionRole
    channel: ConnectionChannel
    required_capabilities: tuple[str, ...]
    provider_id: str | None = None
    provider_credential: str | None = None
    request_timeout: float | None = None


class FrontendClient:
    """Curated asynchronous SDK facade; construction and import never start Theater."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        client_id: str,
        role: ConnectionRole | str = ConnectionRole.OPERATOR,
        channel: ConnectionChannel | str = ConnectionChannel.RPC,
        required_capabilities: Sequence[str] = (),
        provider_id: str | None = None,
        provider_credential: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        if not isinstance(client_id, str) or not client_id:
            raise ValueError("client_id must be a non-empty stable string")
        if request_timeout is not None and (
            not isinstance(request_timeout, (int, float))
            or isinstance(request_timeout, bool)
            or request_timeout <= 0
        ):
            raise ValueError("request_timeout must be a positive number or None")
        try:
            configured_role = ConnectionRole(role)
            configured_channel = ConnectionChannel(channel)
        except ValueError as exc:
            raise ValueError("role and channel must be frozen frontend values") from exc
        capabilities = tuple(required_capabilities)
        if any(not isinstance(capability, str) or not capability for capability in capabilities):
            raise ValueError("required_capabilities must contain non-empty strings")
        self._config = FrontendClientConfig(
            socket_path=str(socket_path),
            client_id=client_id,
            role=configured_role,
            channel=configured_channel,
            required_capabilities=capabilities,
            provider_id=provider_id,
            provider_credential=provider_credential,
            request_timeout=float(request_timeout) if request_timeout is not None else None,
        )
        self._lanes: dict[ConnectionLane, FrontendTransport] = {}
        self._handshakes: dict[ConnectionLane, HandshakeResult] = {}
        self._handshake_responses: dict[ConnectionLane, Response] = {}
        self._next_request_ids: dict[ConnectionLane, int] = dict.fromkeys(ConnectionLane, 1)
        self._lane_handshake_locks = {lane: asyncio.Lock() for lane in ConnectionLane}

        from theater.frontend._facades import (
            CatalogsClient,
            ContractClient,
            ControlsClient,
            DiagnosticsClient,
            HealthClient,
            JobsClient,
            OperationsClient,
            ParticipantsClient,
            ProvidersClient,
            RecallClient,
            SchemasClient,
            ScratchpadClient,
            SkillsClient,
            StateClient,
            TrajectoryClient,
            TranscriptsClient,
            UsageClient,
            WorkspacesClient,
        )

        self.contract: ContractClient = ContractClient(self)
        self.schemas: SchemasClient = SchemasClient(self)
        self.health: HealthClient = HealthClient(self)
        self.state: StateClient = StateClient(self)
        self.participants: ParticipantsClient = ParticipantsClient(self)
        self.controls: ControlsClient = ControlsClient(self)
        self.operations: OperationsClient = OperationsClient(self)
        self.jobs: JobsClient = JobsClient(self)
        self.providers: ProvidersClient = ProvidersClient(self)
        self.workspaces: WorkspacesClient = WorkspacesClient(self)
        self.scratchpad: ScratchpadClient = ScratchpadClient(self)
        self.catalogs: CatalogsClient = CatalogsClient(self)
        self.skills: SkillsClient = SkillsClient(self)
        self.transcripts: TranscriptsClient = TranscriptsClient(self)
        self.recall: RecallClient = RecallClient(self)
        self.trajectory: TrajectoryClient = TrajectoryClient(self)
        self.usage: UsageClient = UsageClient(self)
        self.diagnostics: DiagnosticsClient = DiagnosticsClient(self)

    @property
    def config(self) -> FrontendClientConfig:
        return self._config

    @property
    def connected(self) -> bool:
        transport = self._lanes.get(ConnectionLane.INTERACTIVE)
        return transport is not None and transport.connected

    @property
    def handshake_result(self) -> HandshakeResult | None:
        return self._handshakes.get(ConnectionLane.INTERACTIVE)

    @property
    def handshake_response(self) -> Response | None:
        return self._handshake_responses.get(ConnectionLane.INTERACTIVE)

    async def connect(self) -> HandshakeResult:
        """Open and negotiate the interactive lane without starting a daemon or bridge."""
        return await self._ensure_lane(ConnectionLane.INTERACTIVE)

    async def close(self) -> None:
        """Close all SDK-owned lanes without cancelling remote accepted work."""
        transports = tuple(self._lanes.values())
        self._lanes.clear()
        self._handshakes.clear()
        self._handshake_responses.clear()
        for transport in transports:
            await transport.close()

    async def __aenter__(self) -> FrontendClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        meta: Mapping[str, object] | None = None,
        lane: ConnectionLane = ConnectionLane.INTERACTIVE,
    ) -> Response:
        """Issue one catalog-selected request; public facades are its only callers."""
        spec = self._method_spec(method)
        lane = self._resolve_lane(method, lane)
        self._validate_method_access(spec, lane, idempotency_key)
        self._validate_request_objects(params, meta)
        request = self._build_request(method, params, 1, idempotency_key, meta)
        handshake = await self._ensure_lane(lane)
        self._validate_capabilities(spec, handshake)
        request_id = self._reserve_request_id(lane)
        request["id"] = request_id
        transport = self._lanes[lane]
        return await self._perform_request(lane, transport, method, request_id, request)

    async def _perform_request(
        self,
        lane: ConnectionLane,
        transport: FrontendTransport,
        method: str,
        request_id: int,
        request: Mapping[str, object],
    ) -> Response:
        try:
            raw_response = await self._exchange_with_timeout(transport, request, method, request_id)
        except TransportBusy:
            raise
        except (RequestUncertain, TransportProtocolError, FrontendTransportError):
            self._discard_lane(lane, transport)
            raise
        except asyncio.CancelledError:
            self._discard_lane(lane, transport)
            raise
        try:
            return self._decode_response(method, raw_response, request_id)
        except (ResponseCorrelationError, ResponseValidationError):
            self._discard_lane(lane, transport)
            raise

    def _method_spec(self, method: str) -> MethodSpec:
        try:
            return METHOD_CATALOG[method]
        except KeyError as exc:
            raise ClientStateError(
                f"method is not in the frozen public catalog: {method!r}"
            ) from exc

    def _resolve_lane(self, method: str, requested: ConnectionLane) -> ConnectionLane:
        return _lane_for_method(method) if requested is ConnectionLane.INTERACTIVE else requested

    def _validate_method_access(
        self, spec: MethodSpec, lane: ConnectionLane, idempotency_key: str | None
    ) -> None:
        if (
            lane is not ConnectionLane.PROVIDER_CALLBACK
            and self._config.channel is not ConnectionChannel.RPC
        ):
            raise ClientStateError("ordinary frontend methods require an RPC-channel client")
        if self._config.role not in spec.roles:
            raise MethodRoleError(
                f"{spec.name} is unavailable to the configured {self._config.role.value} role"
            )
        self._validate_idempotency(spec, idempotency_key)

    def _validate_request_objects(
        self, params: Mapping[str, object], meta: Mapping[str, object] | None
    ) -> None:
        if not isinstance(params, Mapping) or any(not isinstance(key, str) for key in params):
            raise TypeError("frontend request params must be an object with string keys")
        if meta is not None and (
            not isinstance(meta, Mapping) or any(not isinstance(key, str) for key in meta)
        ):
            raise TypeError("frontend request _meta must be an object with string keys")

    def _validate_capabilities(self, spec: MethodSpec, handshake: HandshakeResult) -> None:
        missing = spec.required_capabilities.difference(handshake.capabilities)
        if missing:
            names = ", ".join(sorted(missing))
            raise CapabilityUnavailable(f"{spec.name} requires unnegotiated capability: {names}")

    def _build_request(
        self,
        method: str,
        params: Mapping[str, object],
        request_id: int,
        idempotency_key: str | None,
        meta: Mapping[str, object] | None,
    ) -> dict[str, object]:
        request: dict[str, object] = {"id": request_id, "method": method, "params": dict(params)}
        if idempotency_key is not None:
            request["idempotency_key"] = idempotency_key
        if meta is not None:
            request["_meta"] = dict(meta)
        try:
            validate_public_request(request)
        except (ValidationError, ValueError, TypeError) as exc:
            raise ClientStateError(f"invalid public request for {method}: {exc}") from exc
        return request

    async def _ensure_lane(self, lane: ConnectionLane) -> HandshakeResult:
        async with self._lane_handshake_locks[lane]:
            existing = self._handshakes.get(lane)
            transport = self._lanes.get(lane)
            if existing is not None and transport is not None and transport.connected:
                return existing
            if (
                lane is ConnectionLane.PROVIDER_CALLBACK
                and self._config.role is not ConnectionRole.PROVIDER
            ):
                raise MethodRoleError("only a provider client can establish a callback lane")
            if (
                lane is not ConnectionLane.PROVIDER_CALLBACK
                and self._config.channel is not ConnectionChannel.RPC
            ):
                raise ClientStateError("ordinary frontend lanes require an RPC channel")
            request_id = self._reserve_request_id(lane)
            request = {
                "id": request_id,
                "method": "frontend.handshake",
                "params": self._handshake_params(lane),
            }
            try:
                validate_public_request(request)
            except (ValidationError, ValueError, TypeError) as exc:
                raise ClientStateError(f"invalid public handshake: {exc}") from exc
            transport = FrontendTransport(self._config.socket_path)
            self._lanes[lane] = transport
            try:
                await transport.connect()
                raw_response = await self._exchange_with_timeout(
                    transport, request, "frontend.handshake", request_id
                )
                response = self._decode_response("frontend.handshake", raw_response, request_id)
                result = HandshakeResult.from_wire(response.result)
                self._validate_handshake(result, transport)
            except BaseException:
                self._discard_lane(lane, transport)
                raise
            self._handshakes[lane] = result
            self._handshake_responses[lane] = response
            return result

    async def _exchange_with_timeout(
        self,
        transport: FrontendTransport,
        request: Mapping[str, object],
        method: str,
        request_id: int,
    ) -> dict[str, object]:
        if self._config.request_timeout is None:
            return await transport._exchange(request)
        try:
            async with asyncio.timeout(self._config.request_timeout):
                return await transport._exchange(request)
        except TimeoutError as exc:
            transport.abort()
            raise RequestTimedOut(method, request_id) from exc

    def _handshake_params(self, lane: ConnectionLane) -> dict[str, object]:
        channel = (
            ConnectionChannel.CALLBACK
            if lane is ConnectionLane.PROVIDER_CALLBACK
            else self._config.channel
        )
        params: dict[str, object] = {
            "api": {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR},
            "client_id": self._config.client_id,
            "role": self._config.role.value,
            "channel": channel.value,
            "required_capabilities": list(self._config.required_capabilities),
        }
        if self._config.provider_id is not None:
            params["provider_id"] = self._config.provider_id
        if self._config.provider_credential is not None:
            params["provider_credential"] = self._config.provider_credential
        return params

    def _validate_handshake(self, result: HandshakeResult, transport: FrontendTransport) -> None:
        if (result.api.major, result.api.minor) != (PUBLIC_API_MAJOR, PUBLIC_API_MINOR):
            raise NegotiationError(
                f"server negotiated unsupported public API {result.api.major}.{result.api.minor}"
            )
        missing = set(self._config.required_capabilities).difference(result.capabilities)
        if missing:
            names = ", ".join(sorted(missing))
            raise NegotiationError(f"server omitted required capability: {names}")
        advertised_limit = result.limits.get("max_frame_bytes")
        if advertised_limit is None:
            return
        if type(advertised_limit) is not int or not 1 <= advertised_limit <= MAX_FRAME_BYTES:
            raise NegotiationError(
                "handshake limits.max_frame_bytes must be within the public ceiling"
            )
        transport.set_frame_limit(advertised_limit)

    def _decode_response(
        self, method: str, raw_response: Mapping[str, object], request_id: int
    ) -> Response:
        self._validate_response_schema(method, raw_response)
        response_id = raw_response.get("id")
        if type(response_id) is not int or not 0 <= response_id <= MAX_EXACT_JSON_INTEGER:
            raise ResponseCorrelationError("response.id must be an interoperable JSON integer")
        if response_id != request_id:
            raise ResponseCorrelationError(
                f"received response id {response_id}, expected request id {request_id}"
            )
        response = Response.from_wire(raw_response)
        if not response.ok:
            raise FrontendResponseError(response)
        return response

    def _validate_response_schema(self, method: str, value: Mapping[str, object]) -> None:
        try:
            validate_public_response(method, value)
        except ValidationError as exc:
            if not _only_forward_enum_errors(method, value):
                raise ResponseValidationError(
                    f"invalid successful response for {method}: {exc}"
                ) from exc

    def _reserve_request_id(self, lane: ConnectionLane) -> int:
        request_id = self._next_request_ids[lane]
        if request_id > MAX_EXACT_JSON_INTEGER:
            raise ClientStateError("frontend request id space is exhausted; create a fresh client")
        self._next_request_ids[lane] = request_id + 1
        return request_id

    def _validate_idempotency(self, spec: MethodSpec, key: str | None) -> None:
        if spec.idempotency_required and key is None:
            raise IdempotencyRequirementError(f"{spec.name} requires a top-level idempotency_key")
        if not spec.idempotency_required and key is not None:
            raise IdempotencyRequirementError(f"{spec.name} does not accept an idempotency_key")
        if key is not None and (not isinstance(key, str) or not key):
            raise IdempotencyRequirementError("idempotency_key must be a non-empty string")

    def _discard_lane(self, lane: ConnectionLane, transport: FrontendTransport) -> None:
        if self._lanes.get(lane) is transport:
            self._lanes.pop(lane, None)
            self._handshakes.pop(lane, None)
            self._handshake_responses.pop(lane, None)
        transport.abort()


def _only_forward_enum_errors(method: str, value: Mapping[str, object]) -> bool:
    """Allow only additive enum values after strict envelope validation succeeds."""
    if value.get("ok") is not True:
        return False
    spec = METHOD_CATALOG[method]
    try:
        validator_for(spec.response_schema_id).validate(value)
    except ValidationError:
        return False
    errors = list(validator_for(spec.result_schema_id).iter_errors(value.get("result")))
    leaves = [leaf for error in errors for leaf in _leaf_errors(error)]
    return bool(leaves) and all(leaf.validator == "enum" for leaf in leaves)


def _leaf_errors(error: ValidationError) -> tuple[ValidationError, ...]:
    if not error.context:
        return (error,)
    return tuple(leaf for child in error.context for leaf in _leaf_errors(child))


def _lane_for_method(method: str) -> ConnectionLane:
    if method in {"frontend.operations.await", "frontend.jobs.await"}:
        return ConnectionLane.OPERATION_WAIT
    if method in {"frontend.state.follow", "frontend.trajectory.follow"}:
        return ConnectionLane.STATE_FOLLOW
    return ConnectionLane.INTERACTIVE


__all__ = [
    "CapabilityUnavailable",
    "ClientStateError",
    "ConnectionLane",
    "FrontendClient",
    "FrontendClientConfig",
    "FrontendClientError",
    "FrontendResponseError",
    "IdempotencyRequirementError",
    "MethodRoleError",
    "NegotiationError",
    "RequestTimedOut",
    "ResponseCorrelationError",
    "ResponseValidationError",
]
