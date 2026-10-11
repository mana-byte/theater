"""Built-in strict schemas; a job snapshots its schema, so editing one never rewrites history.

Each preset is self-contained for a caller that never reads the transcript. Extra keys are
allowed at every level; the preset answer cap bounds them.
"""

from __future__ import annotations

_CALLER = (
    "The caller reads only this JSON, never your transcript: make every field self-contained "
    "and cite exact paths, lines, commands, and hashes."
)


def _text(limit: int, description: str | None = None) -> dict:
    schema: dict = {"type": "string", "maxLength": limit}
    if description is not None:
        schema["description"] = description
    return schema


def _texts(items: int, limit: int, description: str | None = None) -> dict:
    schema: dict = {"type": "array", "maxItems": items, "items": _text(limit)}
    if description is not None:
        schema["description"] = description
    return schema


def _list(items: int, shape: dict, description: str | None = None) -> dict:
    schema: dict = {"type": "array", "maxItems": items, "items": shape}
    if description is not None:
        schema["description"] = description
    return schema


_CHECK = {
    "type": "object",
    "required": ["command", "result"],
    "properties": {
        "command": _text(300),
        "result": {"enum": ["pass", "fail", "skipped", "not_run"]},
        "detail": _text(500, "counts, failing test names, or why it was not run"),
    },
}
_FACT = {
    "type": "object",
    "required": ["claim"],
    "properties": {
        "claim": _text(600),
        "evidence": _text(600, "path:line with a short quote, command with output, or URL"),
        "basis": {"enum": ["observed", "inferred"]},
    },
}
_SIDE_EFFECTS = _texts(
    20,
    300,
    "anything you changed outside the requested files or did externally: installs, generated "
    "directories, other checkouts",
)

PRESET_SCHEMAS: dict[str, dict] = {
    "delegate": {
        "type": "object",
        "description": _CALLER,
        "required": ["status", "summary", "blockers"],
        "properties": {
            "status": {
                "enum": ["done", "partial", "blocked"],
                "description": (
                    "done means everything requested is finished; partial means the rest is "
                    "in limitations; blocked means you need the caller"
                ),
            },
            "summary": _text(2000, "what you did or found, and the outcome"),
            "blockers": _texts(
                10,
                500,
                "what stops you and exactly what the caller must decide or provide; "
                "empty when none",
            ),
            "commit": {
                "type": ["string", "null"],
                "maxLength": 64,
                "description": "null when nothing was committed",
            },
            "branch": _text(200),
            "changed_paths": _texts(200, 300),
            "checks": _list(30, _CHECK),
            "findings": _list(30, _FACT, "the answers or facts the caller asked for"),
            "decisions": _texts(
                20, 500, "choices you made where the request was silent, each with its reason"
            ),
            "limitations": _texts(20, 500, "unfinished items, known gaps, residual risks"),
            "side_effects": _SIDE_EFFECTS,
        },
    },
    "review": {
        "type": "object",
        "description": _CALLER,
        "required": ["verdict", "reviewed", "summary", "findings"],
        "properties": {
            "verdict": {
                "enum": ["approve", "changes_requested", "inconclusive"],
                "description": (
                    "approve means no blocker findings; changes_requested means at least one "
                    "blocker; inconclusive means the review could not be completed, with the "
                    "reason in summary"
                ),
            },
            "reviewed": _text(
                300,
                "exactly what was reviewed: commit or ref and its base, or document and version",
            ),
            "summary": _text(1500),
            "findings": _list(
                30,
                {
                    "type": "object",
                    "required": ["severity", "issue"],
                    "properties": {
                        "severity": {"enum": ["blocker", "major", "minor", "nit"]},
                        "issue": _text(800, "the defect and its failure mode"),
                        "id": _text(20, "e.g. F1 for follow-up rounds"),
                        "location": _text(300, "path:line, section, or other locator"),
                        "evidence": _text(
                            800, "reproduction: command and output, quoted code, or failing input"
                        ),
                        "fix": _text(600, "smallest correct change"),
                    },
                },
                "most severe first; may be empty",
            ),
            "checks": _list(30, _CHECK),
            "not_checked": _texts(20, 300),
            "requirements": _list(
                40,
                {
                    "type": "object",
                    "required": ["requirement", "status"],
                    "properties": {
                        "requirement": _text(300),
                        "status": {"enum": ["met", "partial", "unmet", "not_checked"]},
                        "evidence": _text(500),
                    },
                },
            ),
            "side_effects": _SIDE_EFFECTS,
        },
    },
    "debate": {
        "type": "object",
        "description": _CALLER,
        "required": [
            "verdict",
            "position",
            "strongest_evidence",
            "weakest_assumption",
            "position_change",
            "remaining_blocker",
        ],
        "properties": {
            "verdict": {
                "enum": ["agree", "disagree", "uncertain"],
                "description": "your stance on the proposition under debate",
            },
            "position": _text(1200, "your current position, understandable without the prompt"),
            "strongest_evidence": _text(
                800, "the single most decisive fact with its source, marked observed or inferred"
            ),
            "weakest_assumption": _text(600),
            "position_change": _text(
                600,
                '"initial" on your first exchange; otherwise "unchanged" or what changed and why',
            ),
            "remaining_blocker": _text(600, "empty string when none"),
            "supporting_evidence": _list(10, _FACT),
            "opposing_argument": _text(800, "the strongest argument against your position"),
            "recommendation": _text(1200, "the concrete decision or change you propose"),
            "failure_scenario": _text(800),
        },
    },
}
