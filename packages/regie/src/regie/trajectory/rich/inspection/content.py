"""Readable, theme-safe renderables for any detail value a harness reports.

Content is sniffed rather than trusted. Styles are foreground-only so text sits on
the panel's own background whatever the theme.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import ClassVar

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.markdown import Heading, Markdown
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax, SyntaxTheme
from rich.text import Text
from rich.theme import Theme

from regie.trajectory.domain import ContentFormat, sanitize_text
from regie.trajectory.ui_constants import (
    TRAJECTORY_DETAIL_FOLD_LINES,
    TRAJECTORY_INLINE_LIST_ITEMS,
    TRAJECTORY_JSON_FORMAT_MAX_DEPTH,
    TRAJECTORY_JSON_STRING_BLOCK_MIN_CHARS,
)

# Records escape control characters, so terminal colours arrive as literal "\x1b[…m".
_ANSI = re.compile(r"(?:\x1b|\\x1b)\[[0-9;?]*[A-Za-z]")
_MARKDOWN_BLOCK = re.compile(r"(?m)^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|```|~~~|\|.+\|\s*$)")
_MARKDOWN_INLINE = re.compile(r"(?:\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^]\n]+\]\([^)\n]+\))")
_DIFF_HEADER = re.compile(r"(?m)^(?:@@ .* @@|--- \S|\+\+\+ \S|diff --git )")


_PLAIN = Style()
_DIM = Style(dim=True)


@dataclass(frozen=True, slots=True)
class Palette:
    """Foreground-only styles resolved from the active Textual theme."""

    text: Style = _PLAIN
    muted: Style = _DIM
    accent: Style = _PLAIN
    key: Style = _PLAIN
    string: Style = _PLAIN
    number: Style = _PLAIN
    error: Style = _PLAIN
    success: Style = _PLAIN

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            style = getattr(self, name)
            if style.bgcolor is not None:  # never paint over the panel background
                object.__setattr__(self, name, _foreground(style))


def _foreground(style: Style) -> Style:
    return Style(
        color=style.color,
        bold=style.bold,
        dim=style.dim,
        italic=style.italic,
        underline=style.underline,
        strike=style.strike,
        meta=style.meta or None,
    )


class _ForegroundTheme(SyntaxTheme):
    """Map Pygments tokens to palette foregrounds, leaving the background alone."""

    def __init__(self, palette: Palette) -> None:
        self._palette = palette

    def get_style_for_token(self, token_type) -> Style:
        name = str(token_type)
        palette = self._palette
        if name.startswith("Token.Comment"):
            return palette.muted
        if name.startswith(("Token.Error", "Token.Generic.Deleted")):
            return palette.error
        if name.startswith(("Token.Literal.String", "Token.Generic.Inserted")):
            return palette.string
        if name.startswith(("Token.Literal.Number", "Token.Keyword.Constant")):
            return palette.number
        if name.startswith(("Token.Name.Tag", "Token.Name.Attribute", "Token.Generic.Heading")):
            return palette.key
        if name.startswith(("Token.Keyword", "Token.Name.Function", "Token.Generic.Subheading")):
            return palette.accent
        return palette.text

    def get_background_style(self) -> Style:
        return Style()


def unwrap(value: object, depth: int = 0) -> object:
    """Decode JSON hidden in strings and MCP `content[].text` envelopes, recursively."""
    if depth >= TRAJECTORY_JSON_FORMAT_MAX_DEPTH:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            decoded = lenient_json(stripped)
            if isinstance(decoded, (dict, list)):
                return unwrap(decoded, depth + 1)
        return value
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, list) and content and all(_is_text_part(part) for part in content):
            texts = [unwrap(part["text"], depth + 1) for part in content]
            rest = {key: item for key, item in value.items() if key not in {"content", "_meta"}}
            body = texts[0] if len(texts) == 1 else texts
            return {**rest, "content": body} if rest else body
        return {key: unwrap(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [unwrap(item, depth + 1) for item in value]
    return value


_OMISSION = re.compile(r"… (\d+) (?:source )?bytes omitted …")


def lenient_json(value: str) -> object | None:
    """Parse JSON; a preview cut short or cut in the middle is closed at its last member."""
    try:
        return json.loads(value)
    except RecursionError:
        return None
    except ValueError:
        pass
    omitted = _OMISSION.search(value)
    head = value[: omitted.start()] if omitted else value
    cut = _last_member_end(head)
    if cut is None:
        return None
    end, closers = cut
    note = f"… {omitted.group(1)} bytes omitted" if omitted else "… truncated"
    member = f', "{note}"' if closers[-1:] == ("]",) else f', "{note}": ""' if closers else ""
    try:
        return json.loads(head[:end].rstrip().rstrip(",") + member + "".join(reversed(closers)))
    except (RecursionError, ValueError):
        return None


def _last_member_end(value: str) -> tuple[int, tuple[str, ...]] | None:
    """The end of the last complete member outside strings, with its open brackets."""
    stack: list[str] = []
    cut: tuple[int, tuple[str, ...]] | None = None
    in_string = escaped = False
    for index, char in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and stack:
            stack.pop()
            cut = (index + 1, tuple(stack))
        elif char == "," and stack:
            cut = (index, tuple(stack))
    return cut


def _is_text_part(part: object) -> bool:
    return isinstance(part, dict) and part.get("type", "text") == "text" and "text" in part


def lexer_for_path(path: str | None) -> str | None:
    """A Pygments lexer name for a file path, when its extension is recognisable."""
    if not path:
        return None
    try:
        from pygments.lexers import get_lexer_for_filename
        from pygments.util import ClassNotFound
    except ImportError:
        return None
    try:
        return get_lexer_for_filename(PurePosixPath(path).name).aliases[0]
    except (ClassNotFound, IndexError):
        return None


NODE_META = "trajectory_detail_node"


def render_content(
    value: str,
    palette: Palette,
    *,
    format: ContentFormat = ContentFormat.TEXT,
    lexer: str | None = None,
    scope: str = "",
    toggled: frozenset[str] = frozenset(),
) -> RenderableType:
    """Pick the richest faithful rendering for one reported value.

    Structured data (JSON, YAML, TOML) becomes a tree whose long branches fold;
    `scope` names its branches and `toggled` flips their default fold.
    """
    if format is ContentFormat.MARKDOWN:
        return _ThemedMarkdown(value, palette)
    data = _structured(value, format, lexer)
    if data is not None:
        return DataView(data, palette, scope, toggled)
    if format is ContentFormat.JSON:
        return _text(value, palette.text)
    if _ANSI.search(value):
        raw = value.replace("\\x1b[", "\x1b[")
        return _strip_backgrounds(Text.from_ansi(raw, no_wrap=False))
    if format is ContentFormat.DIFF or (_DIFF_HEADER.search(value) and "```" not in value):
        return _syntax(value, "diff", palette)
    if format is ContentFormat.CODE or lexer:
        return _syntax(value, lexer or "text", palette)
    if _looks_like_markdown(value):
        return _ThemedMarkdown(value, palette)
    if format is ContentFormat.PATH:
        return _text(value, palette.accent)
    return _text(value, palette.text)


def _structured(value: str, format: ContentFormat, lexer: str | None) -> object | None:
    """Decode JSON, YAML, or TOML into data, or None when the text is not structured."""
    stripped = value.lstrip()
    if format is ContentFormat.JSON or stripped[:1] in {"{", "["}:
        decoded = unwrap(value)
        if isinstance(decoded, (dict, list)) or format is ContentFormat.JSON:
            return decoded if isinstance(decoded, (dict, list)) else None
    toml_like = _TOML_TABLE.search(value) and _TOML_KEY.search(value)
    if lexer == "toml" or (lexer is None and toml_like):
        return _parse_toml(value)
    if lexer == "yaml" or (lexer is None and _looks_like_yaml(value)):
        return _parse_yaml(value)
    return None


_TOML_TABLE = re.compile(r"(?m)^\[\[?[\w.\-\"]+\]\]?\s*$")
_TOML_KEY = re.compile(r"(?m)^[\w\-\"]+\s*=\s*\S")
_YAML_LINE = re.compile(r"^\s*(?:- |[\w\-\"'. ]+:(?:\s|$))")


def _looks_like_yaml(value: str) -> bool:
    """Mostly `key: value` or `- item` lines, with at least one nested level."""
    lines = [
        line for line in value.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) < 3 or "```" in value:
        return False
    keyed = sum(bool(_YAML_LINE.match(line)) for line in lines)
    return keyed / len(lines) >= 0.8 and any(line[:1] in {" ", "-"} for line in lines[1:])


def _parse_toml(value: str) -> object | None:
    import tomllib

    try:
        data = tomllib.loads(value)
    except (tomllib.TOMLDecodeError, RecursionError):
        return None
    return unwrap(data) if data else None


def _parse_yaml(value: str) -> object | None:
    try:
        import yaml
    except ImportError:
        return None
    try:
        data = yaml.safe_load(value)
    except (yaml.YAMLError, RecursionError):
        return None
    return unwrap(data) if isinstance(data, (dict, list)) and data else None


def render_data(value: object, palette: Palette) -> RenderableType:
    return DataView(value, palette)


Lines = list[list[Segment]]


class DataView:
    """A YAML-like tree of keys and typed values; branches of 20+ lines fold.

    Long branches start folded; a branch whose id is in `toggled` flips. Every
    foldable line carries its id in NODE_META so the canvas can navigate to it.
    """

    def __init__(
        self,
        value: object,
        palette: Palette,
        scope: str = "",
        toggled: frozenset[str] = frozenset(),
    ) -> None:
        self.value = value
        self.palette = palette
        self.scope = scope
        self.toggled = toggled
        self._defaults: dict[str, bool] = {}
        self._measuring = False

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # Folding defaults come from the fully folded layout, so expanding a branch
        # never makes an ancestor fold itself.
        self._defaults = {}
        self._measuring = True
        self._lines(self.value, "$", console, options.max_width, depth=0)
        self._measuring = False
        for line in self._lines(self.value, "$", console, options.max_width, depth=0):
            yield from line
            yield Segment.line()

    def _lines(
        self, value: object, path: str, console: Console, width: int, *, depth: int
    ) -> Lines:
        palette = self.palette
        if isinstance(value, dict) and value and depth < TRAJECTORY_JSON_FORMAT_MAX_DEPTH:
            lines: Lines = []
            for key, item in value.items():
                label = Text(sanitize_text(str(key)), style=palette.key)
                lines.extend(
                    self._entry(label, item, _child(path, key), console, width, depth=depth)
                )
            return lines
        if isinstance(value, list) and value and depth < TRAJECTORY_JSON_FORMAT_MAX_DEPTH:
            lines = []
            for index, item in enumerate(value):
                label = Text("•", style=palette.muted)
                lines.extend(
                    self._entry(
                        label, item, f"{path}[{index}]", console, width, depth=depth, bullet=True
                    )
                )
            return lines
        return _render(console, _scalar(value, palette), width)

    def _entry(
        self,
        label: Text,
        item: object,
        path: str,
        console: Console,
        width: int,
        *,
        depth: int,
        bullet: bool = False,
    ) -> Lines:
        palette = self.palette
        inline = _inline(item, palette)
        if inline is not None:
            line = label.append(" " if bullet else ": ", style=palette.muted).append_text(inline)
            return _render(console, line, width)
        child_width = max(1, width - 2)
        if isinstance(item, str):
            children = _render(console, render_content(item, palette), child_width)
        else:
            children = self._lines(item, path, console, child_width, depth=depth + 1)
        if bullet and isinstance(item, dict) and len(children) < TRAJECTORY_DETAIL_FOLD_LINES:
            # YAML style: a short mapping's first key shares its bullet's line.
            first = _render(console, label.append(" "), width)[0]
            return [first + children[0], *(_indent(line) for line in children[1:])]
        node = f"{self.scope}{path}"
        foldable = len(children) >= TRAJECTORY_DETAIL_FOLD_LINES
        if self._measuring:
            self._defaults[node] = foldable
            folded = foldable
        else:
            folded = foldable and self._defaults.get(node, False) != (node in self.toggled)
        head = Text()
        if foldable:
            head.append("▸ " if folded else "▾ ", style=palette.accent)
        head.append_text(label)
        if not bullet:
            head.append(":", style=palette.muted)
        if folded:
            head.append(f"  {len(children)} lines", style=palette.muted)
        if foldable:
            head.stylize(Style(meta={NODE_META: node}))
        lines = _render(console, head, width)
        return lines if folded else [*lines, *(_indent(line) for line in children)]


def _child(path: str, key: object) -> str:
    return f"{path}.{key}" if isinstance(key, str) and key.isidentifier() else f"{path}[{key!r}]"


def _indent(line: list[Segment]) -> list[Segment]:
    return [Segment("  "), *line]


def _render(console: Console, renderable: RenderableType, width: int) -> Lines:
    options = console.options.update(width=max(1, width), height=None)
    return [list(line) for line in console.render_lines(renderable, options, pad=False)]


def _inline(value: object, palette: Palette) -> Text | None:
    """Scalars and short scalar lists stay on their key's line."""
    if isinstance(value, str):
        return None if _is_block(value) else _scalar(value, palette)
    if not isinstance(value, (dict, list)) or not value:
        return _scalar(value, palette)
    if not isinstance(value, list) or len(value) > TRAJECTORY_INLINE_LIST_ITEMS:
        return None
    if any(
        isinstance(item, (dict, list)) or (isinstance(item, str) and _is_block(item))
        for item in value
    ):
        return None
    inline = Text("[", style=palette.muted)
    for index, item in enumerate(value):
        if index:
            inline.append(", ", style=palette.muted)
        inline.append_text(_scalar(item, palette))
    return inline.append("]", style=palette.muted)


def _scalar(value: object, palette: Palette) -> Text:
    if isinstance(value, str):
        return _text(value, palette.string)
    if isinstance(value, bool) or value is None:
        return Text(json.dumps(value), style=palette.number)
    if isinstance(value, (int, float)):
        return Text(str(value), style=palette.number)
    return Text(json.dumps(value, ensure_ascii=False), style=palette.muted)


def _is_block(value: str) -> bool:
    return "\n" in value or len(value) >= TRAJECTORY_JSON_STRING_BLOCK_MIN_CHARS


def _looks_like_markdown(value: str) -> bool:
    return bool(_MARKDOWN_BLOCK.search(value) or _MARKDOWN_INLINE.search(value))


def _text(value: str, style: Style) -> Text:
    return Text(sanitize_text(value), style=style, overflow="fold")


def _strip_backgrounds(text: Text) -> Text:
    plain = Text(sanitize_text(text.plain), overflow="fold")
    for span in text.spans:
        style = span.style if isinstance(span.style, Style) else Style.parse(str(span.style))
        plain.stylize(_foreground(style), span.start, span.end)
    return plain


def _syntax(value: str, lexer: str, palette: Palette) -> Syntax:
    return Syntax(sanitize_text(value), lexer, theme=_ForegroundTheme(palette), word_wrap=True)


class _LeftHeading(Heading):
    """Headings left-aligned and unboxed, like the rest of the panel."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        self.text.justify = "left"
        if self.tag in {"h1", "h2"}:
            yield Text()
        yield self.text


class _PanelMarkdown(Markdown):
    elements: ClassVar[dict[str, type]] = {**Markdown.elements, "heading_open": _LeftHeading}


@dataclass(frozen=True, slots=True)
class _ThemedMarkdown:
    value: str
    palette: Palette

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        markdown = _PanelMarkdown(
            sanitize_text(self.value), style=self.palette.text, hyperlinks=False
        )
        markdown.code_theme = _ForegroundTheme(self.palette)  # type: ignore[assignment]
        markdown.inline_code_theme = _ForegroundTheme(self.palette)  # type: ignore[assignment]
        with console.use_theme(_markdown_theme(self.palette)):
            yield from console.render(markdown, options)


def _markdown_theme(palette: Palette) -> Theme:
    text, muted, accent = palette.text, palette.muted, palette.accent
    return Theme(
        {
            "markdown.paragraph": text,
            "markdown.text": text,
            "markdown.em": text + Style(italic=True),
            "markdown.emph": text + Style(italic=True),
            "markdown.strong": text + Style(bold=True),
            "markdown.code": palette.string,
            "markdown.code_block": text,
            "markdown.block_quote": muted,
            "markdown.list": text,
            "markdown.item": text,
            "markdown.item.bullet": accent + Style(bold=True),
            "markdown.item.number": accent,
            "markdown.hr": muted,
            "markdown.h1.border": accent,
            "markdown.h1": accent + Style(bold=True),
            "markdown.h2": accent + Style(bold=True),
            "markdown.h3": accent + Style(bold=True),
            "markdown.h4": accent,
            "markdown.h5": text + Style(italic=True),
            "markdown.h6": muted,
            "markdown.link": accent,
            "markdown.link_url": accent + Style(underline=True),
            "markdown.s": text + Style(strike=True),
            "markdown.table.border": muted,
            "markdown.table.header": accent + Style(bold=True),
            "markdown.kbd": accent + Style(bold=True),
        }
    )


__all__ = [
    "NODE_META",
    "DataView",
    "Palette",
    "lenient_json",
    "lexer_for_path",
    "render_content",
    "render_data",
    "unwrap",
]
