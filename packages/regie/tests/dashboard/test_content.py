"""Dashboard content: sentence and tip corpora, harness availability marks and animated text."""

from __future__ import annotations

import pytest
from regie.dashboard.content import (
    animated_text_content,
    dashboard_tip_window_content,
    harness_availability_content,
    pretty_qualifier_range,
    sentence_parts,
)
from regie.dashboard.widgets import WelcomeDashboard
from regie.ui_constants import (
    REGIE_DASHBOARD_SENTENCES,
    REGIE_DASHBOARD_TIP_CURSOR_STYLE,
    REGIE_DASHBOARD_TIPS,
)

from theater.frontend.dto.catalogs import HarnessCatalogEntry


def _text(parts) -> str:
    return "".join(part if isinstance(part, str) else part[0] for part in parts)


def _styles(parts) -> list[str]:
    return [part[1] for part in parts if isinstance(part, tuple)]


def test_sentence_corpus_has_at_least_30_distinct_highlighted_entries():
    texts = [_text(parts) for parts in REGIE_DASHBOARD_SENTENCES]
    assert len(texts) >= 30
    assert len(set(texts)) == len(texts)
    assert all(texts)
    highlighted = (
        any("accent" in style for style in _styles(parts)) for parts in REGIE_DASHBOARD_SENTENCES
    )
    assert all(highlighted)


def test_sentence_corpus_highlights_imagine_and_building_where_present():
    for parts in REGIE_DASHBOARD_SENTENCES:
        text = _text(parts)
        highlighted = [part[0] for part in parts if isinstance(part, tuple)]
        if "imagine" in text:
            assert "imagine" in highlighted
        if "building" in text:
            assert "building" in highlighted


def test_tip_corpus_uses_categories_and_highlighted_actions():
    assert len(REGIE_DASHBOARD_TIPS) >= 30
    for parts in REGIE_DASHBOARD_TIPS:
        assert " · " in _text(parts)
        styles = _styles(parts)
        assert any("text-muted" in style for style in styles)
        assert any("accent" in style for style in styles)


def test_tip_corpus_covers_regie_capabilities():
    corpus = "\n".join(_text(parts) for parts in REGIE_DASHBOARD_TIPS)
    for phrase in (
        "h/j/k/l",
        "past the last agent",
        "Enter",
        "H/L",
        "<prefix> h",
        "left-click",
        "right-click",
        "usage tile",
        "day, week, and month",
        "per-model details",
        "Ctrl+P",
        "fuzzy search",
        "diagnostic views",
        "duration order",
        "live tail",
        "copies the selected span",
        "lost trajectory",
        "participant link",
        "collapse or expand",
        "Spawn",
        "Resume",
        "coordination traffic",
        "color scheme",
        "kills",
        "daemon and agent sessions keep running",
        "regie.cost_window",
        "click this panel",
    ):
        assert phrase in corpus


def test_configured_sentences_replace_the_builtin_corpus_and_may_disable_it():
    assert sentence_parts(["make it clear", "keep it small"]) == (
        ("make it clear",),
        ("keep it small",),
    )
    assert sentence_parts([]) == ()


def test_harness_availability_uses_compact_marks_and_muted_failures():
    content = harness_availability_content(
        [
            {"name": "codex", "installed": True, "error": None},
            {"name": "vibe", "installed": False, "error": None},
            {"name": "broken", "installed": True, "error": "import failed"},
        ]
    )
    assert str(content) == "✓ codex\n✗ vibe\n✗ broken — import failed"
    assert [span.style for span in content.spans] == [
        "$success",
        "$text-muted",
        "$text-muted",
        "$warning dim",
    ]


def test_dashboard_catalog_requires_installation_and_compatibility_not_provider_readiness():
    def entry(name: str, *, compatible: bool, provider_ready: bool) -> HarnessCatalogEntry:
        return HarnessCatalogEntry.from_wire(
            {
                "name": name,
                "installed": True,
                "compatible": compatible,
                "supported_wiring": ["tmux"],
                "requires_terminal": True,
                "provider_ready": provider_ready,
                "launch_available": compatible and provider_ready,
                "reason": None if compatible else "incompatible",
                "detail": None if compatible else "plugin failed to load",
            }
        )

    dashboard = WelcomeDashboard()
    dashboard.show_catalog(
        (
            entry("ready-later", compatible=True, provider_ready=False),
            entry("broken", compatible=False, provider_ready=True),
        )
    )

    assert dashboard._harnesses == [
        {
            "name": "ready-later",
            "icon": None,
            "binary": None,
            "installed": True,
            "available": True,
            "error": None,
            "native_compatibility": None,
        },
        {
            "name": "broken",
            "icon": None,
            "binary": None,
            "installed": True,
            "available": False,
            "error": "plugin failed to load",
            "native_compatibility": None,
        },
    ]


def test_pi_harness_availability_shows_beta_flag():
    content = harness_availability_content([{"name": "pi", "installed": True, "error": None}])

    assert str(content) == "✓ pi β"
    assert [span.style for span in content.spans] == ["$success", "$warning dim"]


def test_harness_availability_shows_native_qualification():
    content = harness_availability_content(
        [
            {
                "name": "codex",
                "installed": True,
                "error": None,
                "native_compatibility": {
                    "status": "outside-qualified-range",
                    "installed_version": "0.158.0",
                    "qualified_range": "==0.154.0",
                    "wiring": "legacy",
                    "routes": {
                        "send": "tmux",
                        "steer": "tmux",
                        "queue_followup": "tmux",
                        "interrupt": "tmux",
                    },
                },
            },
            {
                "name": "opencode",
                "installed": True,
                "error": None,
                "native_compatibility": {
                    "status": "native-compatible",
                    "installed_version": "2.0.18",
                    "qualified_range": None,
                    "wiring": "native",
                    "routes": {
                        "send": "native",
                        "steer": "native",
                        "interrupt": "tmux",
                    },
                },
            },
            {
                "name": "pi",
                "installed": True,
                "error": None,
                "native_compatibility": {
                    "status": "native-compatible",
                    "installed_version": "0.84.4",
                    "qualified_range": None,
                    "wiring": "native",
                    "routes": {
                        "send": "native",
                        "steer": "unavailable",
                        "interrupt": "native",
                    },
                },
            },
            {
                "name": "vibe",
                "installed": True,
                "error": None,
                "native_compatibility": {
                    "status": "legacy-only",
                    "installed_version": "2.25.8",
                    "qualified_range": None,
                    "wiring": "legacy",
                    "routes": {
                        "send": "tmux",
                        "steer": "tmux",
                        "interrupt": "tmux",
                    },
                },
            },
        ]
    )

    assert str(content) == (
        "✓ codex 0.158.0 — legacy · native needs ==0.154.0\n"
        "    send tmux · steer tmux · interrupt tmux\n"
        "✓ opencode 2.0.18 — native\n"
        "    send native · steer native · interrupt tmux\n"
        "✓ pi β 0.84.4 — native\n"
        "    send native · steer unavailable · interrupt native\n"
        "✓ vibe 2.25.8 — legacy\n"
        "    send tmux · steer tmux · interrupt tmux"
    )
    styles = {span.style for span in content.spans}
    assert {"$success", "$warning", "$success dim", "$warning dim", "$text-muted"} <= styles


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("==0.154.0", "==0.154.0"),
        (">=2.1.248", ">=2.1.248"),
        (">=1.18.29,<1.18.30", "1.18.29"),
        (">=2.0.18,<2.1.0", "2.0.18–2.0.x"),
        (">=3.4.5,<3.4.6 || >=4.0.1,<4.1.0", "3.4.5 · 4.0.1–4.0.x"),
        (">=1.2.3,<1.2.5", "1.2.3–1.2.4"),
        (">=0.153.0,<=0.159.1", "0.153.0–0.159.1"),
        (">=5.6.2,<5.7.1 || >=6.2.3,<6.3.0", "5.6.2–5.7.0 · 6.2.3–6.2.x"),
        (">=1.18.29,<1.19", ">=1.18.29,<1.19"),
        ("nonsense", "nonsense"),
        (None, ""),
        ("", ""),
    ],
)
def test_pretty_qualifier_range_compacts_only_recognisable_pairs(raw, expected):
    assert pretty_qualifier_range(raw) == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            {
                "name": "codex",
                "installed": True,
                "error": None,
                "native_compatibility": {
                    "status": "native-compatible",
                    "installed_version": "0.154.0",
                    "qualified_range": "==0.154.0",
                    "wiring": "native",
                },
            },
            "✓ codex 0.154.0 — native · qualified ==0.154.0",
        ),
        (
            {
                "name": "opencode",
                "installed": True,
                "error": None,
                "native_compatibility": {
                    "status": "native-compatible",
                    "installed_version": "2.0.18",
                    "qualified_range": None,
                    "wiring": None,
                    "routes": {
                        "send": "native",
                        "steer": "unavailable",
                        "interrupt": "native",
                        "settings_update": "native",
                    },
                },
            },
            "✓ opencode 2.0.18 — native\n"
            "    send native · steer unavailable · interrupt native · settings native",
        ),
        (
            {
                "name": "old",
                "installed": True,
                "error": None,
                "native_compatibility": {
                    "status": "unknown",
                    "installed_version": None,
                    "reason": "x" * 80,
                },
            },
            "✓ old — legacy · compatibility unknown (" + "x" * 59 + "…)",
        ),
        (
            {"name": "pi", "installed": False, "error": "executable not found"},
            "✗ pi β — executable not found",
        ),
    ],
)
def test_harness_availability_degrades_when_daemon_details_are_missing(row, expected):
    content = harness_availability_content([row])

    assert str(content) == expected


def test_animated_content_preserves_partial_highlight():
    parts = ("imagine ", ("writing", "$accent bold"), " code")
    content = animated_text_content(parts, 9)
    assert str(content) == "imagine w"
    assert any("accent" in span.style for span in content.spans if span.style)


def test_animated_content_places_cursor_after_visible_prefix():
    parts = ("imagine ", ("writing", "$accent bold"), " code")
    content = animated_text_content(parts, 9, cursor=True)
    assert str(content) == "imagine w█"


def test_animated_content_accepts_dim_tip_cursor_style():
    content = animated_text_content(
        (("Tips: ", "$text-muted dim"), ("click", "$text-accent bold")),
        6,
        cursor=True,
        cursor_style=REGIE_DASHBOARD_TIP_CURSOR_STYLE,
    )
    assert str(content) == "Tips: █"
    assert any("text-muted" in span.style and "dim" in span.style for span in content.spans)


def test_animated_content_at_zero_is_blank_without_cursor():
    assert str(animated_text_content(("hello",), 0)) == ""


def test_tip_window_renders_one_active_and_two_dimmed_lines():
    content = dashboard_tip_window_content(
        (
            (("Tree · ", "$text-muted bold"), ("move", "$text-accent bold")),
            (("Usage · ", "$text-muted bold"), ("inspect", "$text-accent bold")),
            (("Trajectory · ", "$text-muted bold"), ("search", "$text-accent bold")),
        ),
        incoming_visible=13,
        cursor=True,
    )

    assert str(content) == "Tips\n› Tree · move\n  Usage · inspect\n  Trajectory · █"
    styles = [span.style for span in content.spans if span.style]
    assert any("accent" in style and "dim" not in style for style in styles)
    assert any("accent" in style and "dim" in style for style in styles)
