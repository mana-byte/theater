"""Deliberate breakages of a plugin's production wiring, to prove the live suite notices them.

Each mutation wraps the shipped manifest from a local plugin package in the sandbox, using only
public manifest values, so it needs no test hook in production code and no edit when a plugin
changes. They are tooling checks of the suite, never part of a release verdict.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Literal

from tests.support.obligations import Obligation

#: ``fail`` is a proven violation; ``not-pass`` also accepts an inconclusive error (a turn that
#: never completes cannot always be told from a slow one).
Expectation = Literal["fail", "not-pass"]
ANY = "*"
#: Logged by the mutated package when the daemon imports it: proof the mutation was in force.
LOADED_BANNER = "canary %s active"
#: Logged by a mutation that removed something, with the count; zero counts do not match.
DROPPED_BANNER = "canary dropped %d %s"
DROPPED_EFFECT = r"canary dropped [1-9]\d* "

_WRAP_PLANNER = """
def mutate(manifest):
    inner = manifest.launch.planner

    def planner(context):
        {body}

    return replace(manifest, launch=replace(manifest.launch, planner=planner))
"""


@dataclass(frozen=True, slots=True)
class Mutation:
    name: str
    #: Python defining ``mutate(manifest) -> manifest``; ``replace`` is in scope.
    source: str
    #: Obligation ids this must break (``ANY`` for at least one), and how.
    breaks: Mapping[str, Expectation]
    #: Selects only the scenarios that can notice it: a pytest ``-k`` expression.
    select: str
    #: Logged by the mutation when it changed something; absent from every daemon log means the
    #: plugin had nothing for it to break (a planner that writes no files), so nothing is proven.
    effect: str = ""


def _planner(body: str) -> str:
    return _WRAP_PLANNER.format(body=body)


def _drop(label: str, *attributes: str) -> str:
    """Empty launch-plan ``attributes`` and log how much there was to lose."""
    count = " + ".join(f"len(plan.{name})" for name in attributes)
    empty = ", ".join(f"{name}={{}}" for name in attributes)
    return (
        "plan = inner(context)\n"
        "        logging.getLogger('theater.canary').warning(\n"
        f"            {DROPPED_BANNER!r}, {count}, {label!r}\n"
        "        )\n"
        f"        return replace(plan, {empty})"
    )


def _remap_approval(requested: str, launched: str) -> str:
    """Launch with ``launched`` whenever ``requested`` was asked for."""
    return (
        f"approval = {launched!r} if context.approval == {requested!r} else context.approval\n"
        "        return inner(replace(context, approval=approval))"
    )


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "manual-as-yolo",
        _planner(_remap_approval("manual", "yolo")),
        {"mcp:gated": "fail", "edit:gated": "fail"},
        "mcp or edit",
    ),
    Mutation(
        "edits-as-manual",
        _planner(_remap_approval("edits", "manual")),
        {"edit:runs": "fail"},
        "edit",
    ),
    Mutation(
        "model-dropped",
        _planner("return inner(replace(context, model=None))"),
        {"model:model": "fail"},
        "model",
    ),
    Mutation(
        "resume-dropped",
        _planner("return inner(replace(context, resume=None))"),
        {"resume:resume": "not-pass"},
        "resume",
    ),
    Mutation(
        "interrupt-keys-inert",
        dedent(
            """
            def mutate(manifest):
                from theater.harness.contracts import InterruptPlan

                controls = replace(manifest.controls, interrupt=InterruptPlan(keys=("F20",)))
                return replace(manifest, controls=controls)
            """
        ),
        {"interrupt:interrupt": "fail"},
        "interrupt",
    ),
    Mutation(
        "launch-env-dropped",
        _planner(_drop("env", "env")),
        {ANY: "not-pass"},
        "turn",
        DROPPED_EFFECT,
    ),
    Mutation(
        "launch-files-dropped",
        _planner(_drop("files", "files", "private_files")),
        {ANY: "not-pass"},
        "turn",
        DROPPED_EFFECT,
    ),
)


def applicable(mutation: Mutation, required: tuple[Obligation, ...]) -> Mapping[str, Expectation]:
    """The breakage this harness is expected to show: what the mutation breaks and it declares."""
    ids = {item.id for item in required}
    if ANY in mutation.breaks:
        return mutation.breaks
    return {name: how for name, how in mutation.breaks.items() if name in ids}


def package_source(harness: str, mutation: Mutation) -> str:
    """A local plugin package that shadows ``harness`` with its shipped manifest, mutated."""
    return (
        f'"""Canary {mutation.name}: shipped {harness} manifest, deliberately broken."""\n\n'
        "import logging\n"
        "from dataclasses import replace\n\n"
        f"from theater.harness.builtin.plugins.{harness}.manifest import MANIFEST as _SHIPPED\n"
        f"{dedent(mutation.source)}\n"
        "MANIFEST = mutate(_SHIPPED)\n"
        f'logging.getLogger("theater.canary").warning({LOADED_BANNER!r}, {mutation.name!r})\n'
    )


def install(name: str, harness: str, plugins_dir: Path) -> None:
    """Write the mutated package of the named mutation into ``plugins_dir/<harness>``."""
    mutation = next(item for item in MUTATIONS if item.name == name)
    package = plugins_dir / harness
    package.mkdir(parents=True, exist_ok=True)
    (package / "manifest.py").write_text(package_source(harness, mutation), encoding="utf-8")
