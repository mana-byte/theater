"""Readable, theme-safe renderables for any detail value a harness reports.

Content is sniffed rather than trusted: JSON nested in strings and MCP content
envelopes are unwrapped, ANSI, diffs, markdown, and code are recognised in plain
text. Styles carry foreground colours only, so text always sits on the panel's
own background whatever the theme.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markdown import Markdown
from rich.padding import Padding
from rich.style import Style
from rich.syntax import Syntax, SyntaxTheme
from rich.text import Text
from rich.theme import Theme

from regie.trajectory.domain import ContentFormat, sanitize_text
from regie.trajectory.ui_constants import (
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


def render_content(
    value: str,
    palette: Palette,
    *,
    format: ContentFormat = ContentFormat.TEXT,
    lexer: str | None = None,
) -> RenderableType:
    """Pick the richest faithful rendering for one reported value."""
    if format is ContentFormat.JSON or value.lstrip()[:1] in {"{", "["}:  # also JSON in text
        decoded = unwrap(value)
        if not isinstance(decoded, str):
            return render_data(decoded, palette)
        if format is ContentFormat.JSON:
            return _text(value, palette.text)
    if _ANSI.search(value):
        raw = value.replace("\\x1b[", "\x1b[")
        return _strip_backgrounds(Text.from_ansi(raw, no_wrap=False))
    if format is ContentFormat.DIFF or _DIFF_HEADER.search(value):
        return _syntax(value, "diff", palette)
    if format is ContentFormat.CODE or lexer:
        return _syntax(value, lexer or "text", palette)
    if format is ContentFormat.MARKDOWN or _looks_like_markdown(value):
        return _ThemedMarkdown(value, palette)
    if format is ContentFormat.PATH:
        return _text(value, palette.accent)
    return _text(value, palette.text)


def render_data(value: object, palette: Palette) -> RenderableType:
    """A YAML-like tree: keys, typed scalars, and long strings as rendered blocks."""
    return Group(*_data_lines(value, palette, indent=0, depth=0))


def _data_lines(
    value: object, palette: Palette, *, indent: int, depth: int
) -> Iterator[RenderableType]:
    pad = " " * indent
    if isinstance(value, dict) and value and depth < TRAJECTORY_JSON_FORMAT_MAX_DEPTH:
        for key, item in value.items():
            label = Text(f"{pad}{sanitize_text(str(key))}", style=palette.key)
            yield from _entry(label, item, palette, indent=indent, depth=depth)
        return
    if isinstance(value, list) and value and depth < TRAJECTORY_JSON_FORMAT_MAX_DEPTH:
        for item in value:
            if isinstance(item, dict) and item:
                # YAML style: a mapping's first key shares the bullet's line.
                lines = list(_data_lines(item, palette, indent=indent + 2, depth=depth + 1))
                first = lines[0]
                if isinstance(first, Text):
                    lines[0] = Text(f"{pad}• ", style=palette.muted) + first[indent + 2 :]
                yield from lines
                continue
            bullet = Text(f"{pad}•", style=palette.muted)
            yield from _entry(bullet, item, palette, indent=indent, depth=depth, bullet=True)
        return
    yield Text(pad).append_text(_scalar(value, palette))


def _entry(
    label: Text,
    item: object,
    palette: Palette,
    *,
    indent: int,
    depth: int,
    bullet: bool = False,
) -> Iterator[RenderableType]:
    separator = " " if bullet else ": "
    if (inline := _inline_list(item, palette)) is not None:
        yield label.append(separator, style=palette.muted).append_text(inline)
    elif isinstance(item, (dict, list)) and item:
        yield label.append("" if bullet else ":", style=palette.muted)
        yield from _data_lines(item, palette, indent=indent + 2, depth=depth + 1)
    elif isinstance(item, str) and _is_block(item):
        yield label.append("" if bullet else ":", style=palette.muted)
        yield Padding(render_content(item, palette), (0, 0, 0, indent + 2))
    else:
        yield label.append(separator, style=palette.muted).append_text(_scalar(item, palette))


def _inline_list(value: object, palette: Palette) -> Text | None:
    """Short lists of scalars read best on one line: [a, b, c]."""
    if not isinstance(value, list) or not value or len(value) > TRAJECTORY_INLINE_LIST_ITEMS:
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


@dataclass(frozen=True, slots=True)
class _ThemedMarkdown:
    value: str
    palette: Palette

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        markdown = Markdown(sanitize_text(self.value), style=self.palette.text, hyperlinks=False)
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
    "Palette",
    "lenient_json",
    "lexer_for_path",
    "render_content",
    "render_data",
    "unwrap",
]
