"""In-memory GitHub stand-in: real request files, policy trees, refs, issues and a shared PR."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from harness_compat.policy import format_policy, policy_relative
from harness_compat.publish import issue_title
from theater.harness.contracts.version_policy import VersionPolicy

POLICY_PATH = policy_relative("claude").as_posix()


def assert_gh_flags(args: tuple[str, ...]) -> None:
    assert not ("--slurp" in args and {"--jq", "--template"}.intersection(args)), (
        "gh does not support --slurp with --jq or --template"
    )


class CompatGithub:
    def __init__(self, sha: str, policy: VersionPolicy) -> None:
        self.refs = {"main": sha}
        self.trees = {"tree-main": {POLICY_PATH: format_policy(policy), "tracked.txt": "tracked\n"}}
        self.commits = {sha: {"tree": {"sha": "tree-main"}, "parents": []}}
        self.pr: int | None = None
        self.pr_body = ""
        self.pr_title = ""
        self.issues: dict[str, int] = {}
        self.issue_bodies: dict[int, str] = {}
        self.issue_states: dict[int, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.requests: list[dict] = []
        self.bodies: list[str] = []
        self.temporary_paths: list[Path] = []
        self.fail: set[str] = set()

    def seed_pending(self, policy: VersionPolicy, *, pr: bool = True) -> None:
        self.trees["tree-pending"] = {**self.trees["tree-main"], POLICY_PATH: format_policy(policy)}
        self.commits["pending"] = {
            "tree": {"sha": "tree-pending"},
            "parents": [self.refs["main"]],
        }
        self.refs["chore/compat-claude"] = "pending"
        self.pr = 17 if pr else None
        for version in policy.excluded:
            title = issue_title("claude", version)
            number = self.issues.setdefault(title, max(self.issues.values(), default=0) + 1)
            self.issue_bodies.setdefault(number, f"Earlier qualification failed for {version}.\n")

    def policy(self) -> VersionPolicy:
        head = self.refs.get("chore/compat-claude", self.refs["main"])
        return VersionPolicy.from_text(self.trees[self.commits[head]["tree"]["sha"]][POLICY_PATH])

    def changed_files(self, base: str, head: str) -> list[dict]:
        old = self.trees[self.commits[base]["tree"]["sha"]]
        new = self.trees[self.commits[head]["tree"]["sha"]]
        return [
            {
                "filename": name,
                "additions": len(new.get(name, "").splitlines()),
                "deletions": len(old.get(name, "").splitlines()),
            }
            for name in old.keys() | new.keys()
            if old.get(name) != new.get(name)
        ]

    def __call__(self, *args: str) -> str:
        assert_gh_flags(args)
        self.calls.append(args)
        if "--body-file" in args:
            path = Path(args[args.index("--body-file") + 1])
            self.temporary_paths.append(path)
            self.bodies.append(path.read_text())
        body: dict = {}
        if "--input" in args:
            path = Path(args[args.index("--input") + 1])
            self.temporary_paths.append(path)
            body = json.loads(path.read_text())
            self.requests.append(body)
        if " ".join(args[:2]) in self.fail:
            raise RuntimeError(f"{' '.join(args[:2])} unavailable")
        if args[:2] == ("pr", "list"):
            return json.dumps([] if self.pr is None else [{"number": self.pr}])
        if args[0] != "api":
            return ""
        if "--paginate" in args:
            return json.dumps(
                [
                    [
                        {"title": title, "number": n, "state": self.issue_states.get(n, "open")}
                        for title, n in self.issues.items()
                    ]
                ]
            )
        path = args[1].removeprefix("repos/{owner}/{repo}/")
        if path == "issues" or path.startswith("issues/"):
            return json.dumps(self._issue(path, body))
        return json.dumps(self._api(path, body))

    def _issue(self, path: str, body: dict):
        if path == "issues":
            if "issue create" in self.fail:
                raise RuntimeError("issue create unavailable")
            number = max(self.issues.values(), default=0) + 1
            self.issues[body["title"]] = number
        else:
            number = int(path.split("/")[1])
            if body and "issue edit" in self.fail:
                raise RuntimeError("issue edit unavailable")
        if "body" in body:
            self.issue_bodies[number] = body["body"]
            self.bodies.append(body["body"])
        if "state" in body:
            self.issue_states[number] = body["state"]
        return {
            "number": number,
            "body": self.issue_bodies.get(number, ""),
            "state": self.issue_states.get(number, "open"),
        }

    def _api(self, path: str, body: dict):
        if path == "pulls" or path.startswith("pulls/"):
            if path == "pulls" and "pr create" in self.fail:
                raise RuntimeError("pr create unavailable")
            self.pr = None if body.get("state") == "closed" else 17
            self.pr_body = body.get("body", self.pr_body)
            self.pr_title = body.get("title", self.pr_title)
            return {"number": 17}
        if path == "git/ref/heads/main":
            return {"object": {"sha": self.refs["main"]}}
        if path.startswith("git/matching-refs/heads/"):
            prefix = path.removeprefix("git/matching-refs/heads/")
            return [
                {"ref": f"refs/heads/{ref}", "object": {"sha": sha}}
                for ref, sha in self.refs.items()
                if ref.startswith(prefix)
            ]
        if path.startswith("compare/"):
            main, head = path.removeprefix("compare/").split("...")
            base = main if head == main else self.commits[head]["parents"][0]
            return {"merge_base_commit": {"sha": base}, "files": self.changed_files(base, head)}
        if path.startswith("contents/"):
            name, ref = path.removeprefix("contents/").split("?ref=")
            text = self.trees[self.commits[ref]["tree"]["sha"]][name]
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if path.startswith("git/commits/"):
            return self.commits[path.removeprefix("git/commits/")]
        if path == "git/trees":
            key = f"tree-{len(self.trees)}"
            self.trees[key] = {
                **self.trees[body["base_tree"]],
                **{item["path"]: item["content"] for item in body["tree"]},
            }
            return {"sha": key}
        if path == "git/commits":
            key = f"commit-{len(self.commits)}"
            self.commits[key] = {"tree": {"sha": body["tree"]}, "parents": body["parents"]}
            return {"sha": key}
        if path == "git/refs":
            ref = body["ref"].removeprefix("refs/heads/")
            assert ref not in self.refs
            self.refs[ref] = body["sha"]
            return {"object": {"sha": body["sha"]}}
        if path.startswith("git/refs/heads/"):
            ref = path.removeprefix("git/refs/heads/")
            if "ref-update" in self.fail:
                raise RuntimeError("non-fast-forward: concurrent branch update")
            assert body["force"] is False
            assert self.refs[ref] in self.commits[body["sha"]]["parents"]
            self.refs[ref] = body["sha"]
            return {"object": {"sha": body["sha"]}}
        raise AssertionError(f"unexpected GitHub API path {path}")
