"""Theme-aware Rich renderables for trajectory detail values."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.markdown import Markdown
from rich.measure import Measurement
from rich.padding import Padding
from rich.style import Style
from rich.syntax import Syntax, SyntaxTheme
from rich.text import Span, Text
from rich.theme import Theme

from theater.constants.regie_trajectory import (
    TRAJECTORY_JSON_EXPANDED_STRING_LIMIT,
    TRAJECTORY_JSON_FORMAT_MAX_DEPTH,
    TRAJECTORY_JSON_STRING_BLOCK_MIN_CHARS,
)
from theater.regie.trajectory.inspection.links import DETAIL_JSON_TOGGLE_META
from theater.trajectory import ContentFormat

_MARKDOWN_BLOCK = re.compile(r"(?m)^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|```|~~~)")
_MARKDOWN_INLINE = re.compile(r"(?:\*\*[^*]+\*\*|`[^`]+`|\[[^]]+\]\([^)]+\))")


@dataclass(frozen=True, slots=True)
class DetailStyles:
    text: Style
    accent: Style
    code: Style
    muted: Style
    error: Style
    success: Style

    @classmethod
    def fallback(cls, accent: Style) -> DetailStyles:
        return cls(
            text=Style(),
            accent=accent,
            code=Style(),
            muted=Style(dim=True),
            error=Style(),
            success=Style(),
        )


@dataclass(frozen=True, slots=True)
class DetailDocument:
    renderables: tuple[RenderableType, ...]
    copy_text: str

    @property
    def plain(self) -> str:
        return self.copy_text

    @property
    def spans(self) -> tuple[Span, ...]:
        return tuple(
            span
            for renderable in self.renderables
            if isinstance(renderable, Text)
            for span in renderable.spans
        )

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        for renderable in self.renderables:
            if isinstance(renderable, Text):
                for line in renderable.split("\n", allow_blank=True):
                    line.pad_right(options.max_width)
                    yield line
            else:
                yield renderable

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(options.max_width, options.max_width)


class _DetailSyntaxTheme(SyntaxTheme):
    def __init__(self, styles: DetailStyles) -> None:
        self._text = Style(color=styles.text.color)
        self._accent = Style(color=styles.accent.color)
        self._muted = Style(color=styles.muted.color, dim=True)
        self._error = Style(color=styles.error.color)
        self._success = Style(color=styles.success.color)
        self._background = Style(bgcolor=styles.code.bgcolor)

    def get_style_for_token(self, token_type) -> Style:
        name = str(token_type)
        if name.startswith("Token.Comment"):
            return self._muted
        if name.startswith(("Token.Error", "Token.Generic.Deleted")):
            return self._error
        if name.startswith(("Token.String", "Token.Literal.String", "Token.Generic.Inserted")):
            return self._success
        if name.startswith(
            (
                "Token.Keyword",
                "Token.Literal.Number",
                "Token.Name.Function",
                "Token.Name.Tag",
                "Token.Operator",
            )
        ):
            return self._accent
        return self._text

    def get_background_style(self) -> Style:
        return self._background


@dataclass(frozen=True, slots=True)
class _ThemedMarkdown:
    value: str
    styles: DetailStyles

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        syntax_theme = _DetailSyntaxTheme(self.styles)
        markdown = Markdown(
            self.value,
            style=self.styles.text,
            hyperlinks=False,
            inline_code_lexer="text",
        )
        markdown.code_theme = syntax_theme  # type: ignore[assignment]
        markdown.inline_code_theme = syntax_theme  # type: ignore[assignment]
        with console.use_theme(_markdown_theme(self.styles)):
            yield from console.render(markdown, options)


@dataclass(frozen=True, slots=True)
class _JsonStringBlock:
    path: str
    value: str


@dataclass(frozen=True, slots=True)
class _ThemedJson:
    value: str
    styles: DetailStyles
    scope: str
    collapsed_paths: frozenset[str]

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        syntax_theme = _DetailSyntaxTheme(self.styles)
        try:
            parsed = json.loads(self.value)
        except (RecursionError, TypeError, ValueError):
            yield Syntax(self.value, "json", theme=syntax_theme, word_wrap=True)
            return
        expanded: list[_JsonStringBlock] = []
        _collect_json_strings(parsed, "$", expanded, depth=0)
        if not expanded:
            yield Syntax(self.value, "json", theme=syntax_theme, word_wrap=True)
            return
        yield from _render_json_node(
            parsed,
            "$",
            {block.path: block for block in expanded},
            self.styles,
            self.collapsed_paths,
            syntax_theme,
            options,
            scope=self.scope,
            depth=0,
            indent=0,
            leading="",
            trailing="",
        )


def _collect_json_strings(
    value: object,
    path: str,
    expanded: list[_JsonStringBlock],
    *,
    depth: int,
) -> None:
    if depth >= TRAJECTORY_JSON_FORMAT_MAX_DEPTH:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _collect_json_strings(
                child,
                _json_child_path(path, key),
                expanded,
                depth=depth + 1,
            )
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _collect_json_strings(
                child,
                f"{path}[{index}]",
                expanded,
                depth=depth + 1,
            )
        return
    if isinstance(value, str) and _expandable_string(value):
        if len(expanded) >= TRAJECTORY_JSON_EXPANDED_STRING_LIMIT:
            return
        expanded.append(_JsonStringBlock(path, value))


def _render_json_node(
    value: object,
    path: str,
    expanded: dict[str, _JsonStringBlock],
    styles: DetailStyles,
    collapsed_paths: frozenset[str],
    syntax_theme: SyntaxTheme,
    options: ConsoleOptions,
    *,
    scope: str,
    depth: int,
    indent: int,
    leading: str,
    trailing: str,
) -> Iterator[RenderableType]:
    block = expanded.get(path)
    if block is not None:
        toggle_key = f"{scope}:{path}"
        collapsed = toggle_key in collapsed_paths
        glyph = "▸" if collapsed else "▾"
        toggle_style = Style(meta={DETAIL_JSON_TOGGLE_META: toggle_key})
        line = Text(style=styles.code + toggle_style)
        prefix = Syntax("", "json", theme=syntax_theme).highlight(leading)
        if prefix.plain.endswith("\n"):
            prefix = prefix[:-1]
        line.append_text(prefix)
        line.append(
            glyph,
            style=styles.accent + Style(bold=True) + toggle_style,
        )
        line.pad_right(options.max_width)
        yield line
        if not collapsed:
            left = min(indent + 2, max(0, options.max_width - 1))
            yield Padding(
                _formatted_string(
                    block.value,
                    styles,
                    scope=toggle_key,
                    collapsed_paths=collapsed_paths,
                ),
                (0, 0, 0, left),
            )
        return
    if depth >= TRAJECTORY_JSON_FORMAT_MAX_DEPTH:
        yield _json_syntax(
            f"{leading}{json.dumps(value, ensure_ascii=False)}{trailing}",
            syntax_theme,
        )
        return
    if isinstance(value, dict):
        yield _json_syntax(f"{leading}{{", syntax_theme)
        items = tuple(value.items())
        for index, (key, child) in enumerate(items):
            yield from _render_json_node(
                child,
                _json_child_path(path, key),
                expanded,
                styles,
                collapsed_paths,
                syntax_theme,
                options,
                scope=scope,
                depth=depth + 1,
                indent=indent + 2,
                leading=f"{' ' * (indent + 2)}{json.dumps(key, ensure_ascii=False)}: ",
                trailing="," if index < len(items) - 1 else "",
            )
        yield _json_syntax(f"{' ' * indent}}}{trailing}", syntax_theme)
        return
    if isinstance(value, list):
        yield _json_syntax(f"{leading}[", syntax_theme)
        for index, child in enumerate(value):
            yield from _render_json_node(
                child,
                f"{path}[{index}]",
                expanded,
                styles,
                collapsed_paths,
                syntax_theme,
                options,
                scope=scope,
                depth=depth + 1,
                indent=indent + 2,
                leading=" " * (indent + 2),
                trailing="," if index < len(value) - 1 else "",
            )
        yield _json_syntax(f"{' ' * indent}]{trailing}", syntax_theme)
        return
    yield _json_syntax(f"{leading}{json.dumps(value, ensure_ascii=False)}{trailing}", syntax_theme)


def _json_syntax(value: str, syntax_theme: SyntaxTheme) -> Syntax:
    return Syntax(value, "json", theme=syntax_theme, word_wrap=True)


def _json_child_path(path: str, key: object) -> str:
    if isinstance(key, str) and key.isidentifier():
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _expandable_string(value: str) -> bool:
    return "\n" in value or len(value) >= TRAJECTORY_JSON_STRING_BLOCK_MIN_CHARS


def _formatted_string(
    value: str,
    styles: DetailStyles,
    *,
    scope: str,
    collapsed_paths: frozenset[str],
) -> RenderableType:
    stripped = value.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            nested = json.loads(value)
        except (RecursionError, TypeError, ValueError):
            pass
        else:
            if isinstance(nested, (dict, list)):
                return _ThemedJson(value, styles, scope, collapsed_paths)
    if _MARKDOWN_BLOCK.search(value) or _MARKDOWN_INLINE.search(value):
        return _ThemedMarkdown(value, styles)
    return Text(value, style=styles.text, overflow="fold")


def _markdown_theme(styles: DetailStyles) -> Theme:
    return Theme(
        {
            "markdown.paragraph": styles.text,
            "markdown.text": styles.text,
            "markdown.em": styles.text + Style(italic=True),
            "markdown.emph": styles.text + Style(italic=True),
            "markdown.strong": styles.text + Style(bold=True),
            "markdown.code": styles.code,
            "markdown.code_block": styles.code,
            "markdown.block_quote": styles.muted,
            "markdown.list": styles.text,
            "markdown.item": styles.text,
            "markdown.item.bullet": styles.accent + Style(bold=True),
            "markdown.item.number": styles.accent,
            "markdown.hr": styles.muted,
            "markdown.h1.border": styles.accent,
            "markdown.h1": styles.accent + Style(bold=True, underline=True),
            "markdown.h2": styles.accent + Style(underline=True),
            "markdown.h3": styles.accent + Style(bold=True),
            "markdown.h4": styles.accent + Style(italic=True),
            "markdown.h5": styles.text + Style(italic=True),
            "markdown.h6": styles.muted,
            "markdown.h7": styles.muted + Style(italic=True),
            "markdown.link": styles.accent,
            "markdown.link_url": styles.accent + Style(underline=True),
            "markdown.s": styles.text + Style(strike=True),
            "markdown.table.border": styles.accent,
            "markdown.table.header": styles.accent + Style(bold=True),
            "markdown.kbd": styles.accent + Style(bold=True),
        }
    )


def formatted_value(
    value: str,
    format: ContentFormat,
    styles: DetailStyles,
    *,
    scope: str = "content",
    collapsed_json_paths: frozenset[str] = frozenset(),
) -> RenderableType:
    syntax_theme = _DetailSyntaxTheme(styles)
    if format is ContentFormat.JSON:
        return _ThemedJson(value, styles, scope, collapsed_json_paths)
    if format is ContentFormat.DIFF:
        return Syntax(value, "diff", theme=syntax_theme, word_wrap=True)
    if format is ContentFormat.CODE:
        return Syntax(value, "text", theme=syntax_theme, word_wrap=True)
    if format is ContentFormat.MARKDOWN:
        return _ThemedMarkdown(value, styles)
    style = styles.accent if format is ContentFormat.PATH else styles.text
    return Text(value, style=style, overflow="fold")


__all__ = ["DetailDocument", "DetailStyles", "formatted_value"]
