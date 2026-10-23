"""Pure request facts shared by compatibility route checks."""

from __future__ import annotations

from dataclasses import dataclass

from .contract import RecordedRequest, Route


@dataclass(frozen=True)
class RouteFacts:
    requests: tuple[RecordedRequest, ...]
    expected: frozenset[Route]
    ignorable: frozenset[Route]

    @staticmethod
    def route(request: RecordedRequest) -> Route:
        return request.method, request.path

    def is_unexpected(self, request: RecordedRequest) -> bool:
        return self.route(request) not in self.expected

    def is_ignorable(self, request: RecordedRequest) -> bool:
        return self.route(request) in self.ignorable

    @property
    def observed_routes(self) -> set[Route]:
        return {self.route(request) for request in self.requests}

    @property
    def unexpected_routes(self) -> set[Route]:
        return {
            self.route(request)
            for request in self.requests
            if self.is_unexpected(request) and not self.is_ignorable(request)
        }

    @property
    def unignored_routes(self) -> set[Route]:
        return {self.route(request) for request in self.requests if not self.is_ignorable(request)}

    @property
    def gaps(self) -> tuple[RecordedRequest, ...]:
        return tuple(
            request
            for request in self.requests
            if not self.is_ignorable(request)
            and (not request.handled or self.is_unexpected(request))
        )
