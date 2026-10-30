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

from harness_compat.support.obligations import Obligation

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

#: ``alter(context)`` applied wherever the manifest receives the launch facts. A harness with a
#: native runtime is launched by its backend planner and driven by its runtime factory, each with
#: the same approval and model, so a canary that only broke the legacy launch planner would pass
#: silently on exactly the harnesses whose production path is the native one.
_ALTER_CONTEXTS = """
def alter(context):
    {body}


def mutate(manifest):
    inner_planner = manifest.launch.planner
    changes = {
        "launch": replace(
            manifest.launch, planner=lambda context: inner_planner(alter(context))
        )
    }
    runtime = manifest.runtime
    if runtime is not None:
        inner_factory = runtime.factory
        wrapped = {"factory": lambda context: inner_factory(alter(context))}
        if runtime.plan is not None:
            inner_plan = runtime.plan
            wrapped["plan"] = lambda context: inner_plan(alter(context))
        changes["runtime"] = replace(runtime, **wrapped)
    return replace(manifest, **changes)
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


def _contexts(body: str) -> str:
    return _ALTER_CONTEXTS.replace("{body}", body)


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
        "    return replace(context, approval=approval)"
    )


def _drop_hook(event: str) -> str:
    """Remove only the selected event's added public-settings entries, retaining user hooks."""
    return dedent(
        """
        def mutate(manifest):
            import json
            from theater.harness.contracts.manifest import HookChannelManifest

            def wrap(channel):
                if not isinstance(channel, HookChannelManifest) or channel.installer is None:
                    return channel
                inner = channel.installer

                def installer(context):
                    overlay = inner(context)
                    replacements = dict(overlay.replacements)
                    dropped = 0
                    for path, text in overlay.replacements.items():
                        original = json.loads(context.public_files[path])
                        updated = json.loads(text)
                        before = original.get("hooks", {}).get(EVENT, [])
                        after = updated.get("hooks", {}).get(EVENT, [])
                        if after[:len(before)] == before and len(after) > len(before):
                            dropped += len(after) - len(before)
                            updated["hooks"][EVENT] = before
                            replacements[path] = json.dumps(updated) + "\\n"
                    logging.getLogger("theater.canary").warning(
                        "canary dropped %d %s", dropped, EVENT
                    )
                    return replace(overlay, replacements=replacements)

                return replace(channel, installer=installer)

            return replace(
                manifest,
                observation=replace(
                    manifest.observation,
                    enrichments=tuple(
                        wrap(channel) for channel in manifest.observation.enrichments
                    ),
                ),
            )
        """
    ).replace("EVENT", repr(event))


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "prompt-submit-hook-dropped",
        _drop_hook("UserPromptSubmit"),
        {"channels:native-hooks/UserPromptSubmit": "fail"},
        "channel",
        DROPPED_EFFECT,
    ),
    Mutation(
        "stop-hook-dropped",
        _drop_hook("Stop"),
        {"channels:native-hooks/Stop": "fail"},
        "channel",
        DROPPED_EFFECT,
    ),
    Mutation(
        "manual-as-yolo",
        _contexts(_remap_approval("manual", "yolo")),
        {"mcp:gated": "fail", "edit:gated": "fail"},
        "mcp or edit",
    ),
    Mutation(
        "edits-as-manual",
        _contexts(_remap_approval("edits", "manual")),
        {"edit:runs": "fail"},
        "edit",
    ),
    Mutation(
        "model-dropped",
        _contexts("return replace(context, model=None)"),
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
        "interrupt-inert",
        dedent(
            """
            def mutate(manifest):
                from theater.harness.contracts import InterruptPlan
                from theater.harness.contracts.runtime import ControlReceipt, DeliveryResult

                changes = {}
                if manifest.controls.interrupt is not None:
                    changes["controls"] = replace(
                        manifest.controls, interrupt=InterruptPlan(keys=("F20",))
                    )
                runtime = manifest.runtime
                if runtime is not None:
                    inner = runtime.factory

                    def factory(context):
                        native = inner(context)

                        async def interrupt(*, operation_id, native_turn_id=None):
                            return ControlReceipt(
                                operation_id=operation_id,
                                result=DeliveryResult.REJECTED,
                                error_code="canary",
                                error="the canary suppressed the native interrupt",
                            )

                        native.interrupt = interrupt
                        return native

                    changes["runtime"] = replace(runtime, factory=factory)
                return replace(manifest, **changes)
            """
        ),
        {"interrupt:interrupt": "fail"},
        "interrupt",
    ),
    Mutation(
        "native-runtime-refused",
        dedent(
            """
            def mutate(manifest):
                from theater.harness.contracts.runtime import RuntimeCompatibility

                if manifest.runtime is None:
                    return manifest

                def probe(context):
                    return RuntimeCompatibility(supported=False, policy="canary", reason="canary")

                return replace(manifest, runtime=replace(manifest.runtime, probe=probe))
            """
        ),
        {
            "runtime:native": "fail",
            "runtime:survival": "fail",
            "runtime:queue": "fail",
            "runtime:settings": "fail",
        },
        "runtime",
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


def applicable(
    mutation: Mutation, required: tuple[Obligation, ...], canaries: tuple[str, ...] = ()
) -> Mapping[str, Expectation]:
    """The breakage this harness is expected to show: what the mutation breaks and it declares."""
    if ANY in mutation.breaks:
        return mutation.breaks if mutation.name in canaries else {}
    ids = {item.id for item in required}
    return {name: how for name, how in mutation.breaks.items() if name in ids}


def undecided(statuses: Mapping[str, str], expected: Mapping[str, Expectation]) -> bool:
    """Whether the run left an obligation this canary needs decided at ``error``.

    ``error`` is no verdict: the suite neither noticed the breakage nor missed it, so only
    another run can tell. A ``pass`` (a miss) or a ``fail`` is a verdict, and lenient (blanket)
    or untouched obligations never ask for another run.
    """
    return any(how == "fail" and statuses.get(name) == "error" for name, how in expected.items())


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
