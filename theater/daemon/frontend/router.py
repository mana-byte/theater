"""Permanent connection classification and curated public method routing."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from theater import protocol
from theater.daemon import workers
from theater.daemon.frontend.handlers import PUBLIC_HANDLERS
from theater.daemon.frontend.handshake import ConnectionContext, negotiate
from theater.daemon.frontend.telemetry import PublicRequestTiming
from theater.daemon.frontend.validation import (
    PublicRequestError,
    correlated_id,
    error_response,
    parse_public_frame,
    success_response,
    validate_request,
)
from theater.frontend.capabilities import METHOD_CATALOG, ConnectionChannel
from theater.frontend.schemas.catalog import BULK_RESPONSE_METHODS
from theater.models import TheaterError

logger = logging.getLogger("theater.daemon.frontend")


class ConnectionMode(StrEnum):
    UNCLASSIFIED = "unclassified"
    PRIVATE = "private"
    PUBLIC = "public"


def _loose_envelope(line: bytes) -> tuple[dict[str, Any] | None, bytes | None]:
    try:
        value = json.loads(line)
    except (UnicodeError, ValueError) as exc:
        return None, protocol.err(0, "bad_request", f"malformed json: {exc}")
    if not isinstance(value, dict):
        return None, protocol.err(0, "bad_request", "request must be a JSON object")
    return value, None


class ConnectionRouter:
    """One connection's immutable private/public classification and identity."""

    def __init__(self, daemon, *, private_methods: Mapping[str, object] | None = None) -> None:
        self._daemon = daemon
        self._private_methods = private_methods
        self.mode = ConnectionMode.UNCLASSIFIED
        self.context: ConnectionContext | None = None

    async def dispatch(self, line: bytes) -> bytes:
        if self.mode is ConnectionMode.PUBLIC:
            return await self._dispatch_public(line)
        envelope, error = _loose_envelope(line)
        if error is not None:
            return error
        assert envelope is not None
        method = envelope.get("method")
        if self.mode is ConnectionMode.PRIVATE:
            if isinstance(method, str) and method.startswith("frontend."):
                return error_response(
                    correlated_id(envelope.get("id")),
                    "wrong_connection_role",
                    "a private connection cannot enter the public frontend API",
                )
            return await self._daemon._dispatch(line)

        if method == "frontend.handshake":
            return await self._dispatch_handshake(line)
        if isinstance(method, str) and method.startswith("frontend."):
            return error_response(
                correlated_id(envelope.get("id")),
                "handshake_required",
                "frontend.handshake must be the first public frame on this connection",
            )
        if isinstance(method, str) and (
            self._private_methods is None or method in self._private_methods
        ):
            self.mode = ConnectionMode.PRIVATE
            return await self._daemon._dispatch(line)
        # Preserve private error shape without letting an unknown name classify
        # the connection; a later valid handshake remains admissible.
        return await self._daemon._dispatch(line)

    async def _dispatch_handshake(self, line: bytes) -> bytes:
        request_id = 0
        try:
            request = parse_public_frame(line)
            request_id = correlated_id(request.get("id"))
            validate_request(request)
            context, result = negotiate(self._daemon, request["params"])
            response = success_response("frontend.handshake", request_id, result)
        except PublicRequestError as exc:
            return error_response(request_id, exc.code, exc.message, details=exc.details)
        except Exception as exc:
            logger.exception("public handshake failed")
            return error_response(request_id, "internal", f"{type(exc).__name__}: {exc}")
        self.context = context
        self.mode = ConnectionMode.PUBLIC
        return response

    async def _dispatch_public(self, line: bytes) -> bytes:
        request_id = 0
        try:
            request = parse_public_frame(line)
            request_id = correlated_id(request.get("id"))
            self._public_method(request)
            validate_request(request)
            method, context, handler = self._admit_public(request)
            slow_ms = None
            if method in {"frontend.participants.spawn", "frontend.participants.terminate"}:
                slow_ms = 0.0
            elif method.endswith((".await", ".follow")):
                slow_ms = float("inf")  # Waiting is expected, not a slow-handler warning.
            with PublicRequestTiming(
                method,
                caller=context.client_id,
                slow_ms=slow_ms,
            ):
                if "idempotency_key" in inspect.signature(handler).parameters:
                    result = handler(
                        self._daemon,
                        context,
                        request["params"],
                        idempotency_key=request["idempotency_key"],
                    )
                else:
                    result = handler(self._daemon, context, request["params"])
                if inspect.isawaitable(result):
                    result = await result
                if method in BULK_RESPONSE_METHODS:
                    return await workers.to_thread(
                        success_response,
                        method,
                        request_id,
                        result,
                        label="frontend.response_validation",
                    )
                return success_response(method, request_id, result)
        except PublicRequestError as exc:
            return error_response(request_id, exc.code, exc.message, details=exc.details)
        except TheaterError as exc:
            details = getattr(exc, "details", None)
            return error_response(
                request_id,
                exc.code,
                str(exc),
                details=details if isinstance(details, Mapping) else None,
            )
        except Exception:
            logger.exception("public handler failed")
            return error_response(request_id, "internal", "the public request could not be served")

    @staticmethod
    def _public_method(request: Mapping[str, Any]) -> str:
        method = request.get("method")
        if not isinstance(method, str):
            raise PublicRequestError("bad_request", "public method must be a string")
        if not method.startswith("frontend."):
            raise PublicRequestError(
                "wrong_connection_role",
                "a public connection cannot call private daemon methods",
            )
        if method == "frontend.handshake":
            raise PublicRequestError(
                "wrong_connection_role",
                "frontend.handshake cannot be repeated on an admitted connection",
            )
        if method not in METHOD_CATALOG:
            raise PublicRequestError("unknown_method", f"unknown public method {method!r}")
        return method

    def _admit_public(self, request: Mapping[str, Any]):
        method = self._public_method(request)
        context = self.context
        assert context is not None
        if context.channel is ConnectionChannel.CALLBACK:
            raise PublicRequestError(
                "wrong_connection_role",
                "a provider callback connection does not accept ordinary RPC requests",
            )
        spec = METHOD_CATALOG[method]
        if context.role not in spec.roles:
            raise PublicRequestError(
                "wrong_connection_role",
                f"{context.role.value} connections cannot call {method}",
            )
        missing = sorted(spec.required_capabilities - context.capabilities)
        if missing:
            raise PublicRequestError(
                "missing_capability",
                f"{method} requires capabilities unavailable on this connection",
                {"missing": missing},
            )
        handler = PUBLIC_HANDLERS.get(method)
        if handler is None:
            raise PublicRequestError(
                "unknown_method", f"public method {method!r} is not implemented"
            )
        return method, context, handler


__all__ = ["ConnectionMode", "ConnectionRouter"]
