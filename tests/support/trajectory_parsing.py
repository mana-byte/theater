"""Read recorded transcript fixtures and parse them into trajectory facts."""

from __future__ import annotations

from tests.support.paths import FIXTURES


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def parse_facts(observer, lines: list[str]):
    return [
        fact
        for index, line in enumerate(lines)
        for fact in observer.parse_record(line, index).trajectory
    ]
