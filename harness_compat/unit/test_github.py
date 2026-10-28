"""Release discovery never returns a partial history and never leaks the token off-host."""

from __future__ import annotations

import http.client
import json

import pytest

from scripts.harness_compat import github


def _release(tag: str, **flags: bool) -> dict:
    return {"tag_name": tag, "prerelease": False, "draft": False, **flags}


def test_only_canonical_stable_releases_survive_ascending_and_unique() -> None:
    found = github.stable_ascending(
        ["1.10.0", "1.2.0", "1.2.0", "1.2.0-beta.1", "01.2.3", "v1.0.0", "2.0", "0.9.9"]
    )

    assert found == ["0.9.9", "1.2.0", "1.10.0"]


def test_every_github_release_page_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        1: [_release(f"rust-v0.{n}.0") for n in range(100)],
        2: [_release(f"rust-v1.{n}.0") for n in range(100)],
        3: [
            _release("rust-v2.0.0"),
            _release("rust-v2.1.0", prerelease=True),
            _release("rust-v2.2.0", draft=True),
            _release("other-v3.0.0"),
        ],
    }
    asked: list[int] = []

    def get(url: str, headers: object = None) -> str:
        page = int(url.rsplit("page=", 1)[1])
        asked.append(page)
        return json.dumps(pages[page])

    monkeypatch.setattr(github, "_get", get)

    found = github.github_release_versions("openai/codex", tag_prefix="rust-v")

    assert asked == [1, 2, 3]
    assert len(found) == 201 and found[0] == "0.0.0" and found[-1] == "2.0.0"


def test_pypi_keeps_yanked_and_unavailable_stable_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "releases": {
            "1.0.0": [{"yanked": False}],
            "1.1.0": [{"yanked": True}],
            "1.2.0": [],
            "1.3.0": [{"yanked": True}, {"yanked": False}],
            "1.4.0rc1": [{"yanked": False}],
        }
    }
    monkeypatch.setattr(github, "_get", lambda url, headers=None: json.dumps(document))

    assert github.pypi_versions("mistral-vibe") == ["1.0.0", "1.1.0", "1.2.0", "1.3.0"]


def test_npm_scoped_packages_are_queried_with_their_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    def get(url: str, headers: object = None) -> str:
        urls.append(url)
        return json.dumps({"versions": {"2.0.1": {}, "2.0.0-rc.1": {}}})

    monkeypatch.setattr(github, "_get", get)

    assert github.npm_versions("@opencode/cli") == ["2.0.1"]
    assert urls == ["https://registry.npmjs.org/@opencode%2Fcli"]


def test_the_token_is_sent_to_the_github_api_and_nowhere_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"[]"

    def urlopen(request: object, timeout: float) -> Response:
        sent.append((request.full_url, dict(request.header_items())))  # type: ignore[attr-defined]
        return Response()

    monkeypatch.setattr(github.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("GITHUB_TOKEN", "secret")

    github._get("https://api.github.com/repos/o/r/releases")
    github._get("https://registry.npmjs.org/pkg")

    assert sent[0][1]["Authorization"] == "Bearer secret"
    assert "Authorization" not in sent[1][1]


class _Dropping:
    """A response whose body keeps cutting off for the first ``drops`` reads."""

    def __init__(self, drops: int) -> None:
        self.drops = drops

    def __enter__(self) -> _Dropping:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        if self.drops:
            self.drops -= 1
            raise http.client.IncompleteRead(b"[", 27_000_000)
        return b"[]"


def test_a_dropped_transfer_is_retried_but_a_persistent_one_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Dropping(drops=2)
    monkeypatch.setattr(github.urllib.request, "urlopen", lambda request, timeout: response)

    assert github._get("https://api.github.com/repos/o/r/releases") == "[]"

    response.drops = github._ATTEMPTS
    with pytest.raises(http.client.IncompleteRead):
        github._get("https://api.github.com/repos/o/r/releases")
