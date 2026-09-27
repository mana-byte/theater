"""The pixel cat's poses, drawn from round shapes and outlined, at any size.

Each pose is laid around the cat's centre column ``cx`` and the pixel row under its feet,
scaled by ``s`` (1 is full size, 22 pixels tall), so a smaller band still gets a whole cat.
"""

from __future__ import annotations

import math
from itertools import pairwise

from regie.ambience.pixels import Canvas

FUR, STRIPE, CREAM, OUTLINE = "#b98050", "#8f6038", "#cdb99b", "#4a3526"
EYE, SHUT, PINK = "#86a07a", "#3d302a", "#b58a8c"
BOWL, KIBBLE, WATER, RIPPLE = "#737b82", "#7a5a3c", "#4f7189", "#6d8fa5"
BED, BED_EDGE = "#76698c", "#5c5272"


def _ellipse(canvas: Canvas, cx: float, cy: float, rx: float, ry: float, colour: str) -> None:
    for y in range(math.floor(cy - ry), math.ceil(cy + ry) + 1):
        for x in range(math.floor(cx - rx), math.ceil(cx + rx) + 1):
            if ((x + 0.5 - cx) / rx) ** 2 + ((y + 0.5 - cy) / ry) ** 2 <= 1:
                canvas[(x, y)] = colour


def _triangle(canvas: Canvas, corners: tuple[tuple[float, float], ...], colour: str) -> None:
    (ax, ay), (bx, by), (cx, cy) = corners
    for y in range(math.floor(min(ay, by, cy)), math.ceil(max(ay, by, cy)) + 1):
        for x in range(math.floor(min(ax, bx, cx)), math.ceil(max(ax, bx, cx)) + 1):
            px, py = x + 0.5, y + 0.5
            d1 = (px - bx) * (ay - by) - (ax - bx) * (py - by)
            d2 = (px - cx) * (by - cy) - (bx - cx) * (py - cy)
            d3 = (px - ax) * (cy - ay) - (cx - ax) * (py - ay)
            if not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0)):
                canvas[(x, y)] = colour


def _limb(canvas: Canvas, points: list[tuple[float, float]], r: float, colour: str) -> None:
    """A thick stroke through ``points``: a tail, a leg."""
    for (x0, y0), (x1, y1) in pairwise(points):
        steps = max(1, int(math.hypot(x1 - x0, y1 - y0) * 2))
        for i in range(steps + 1):
            f = i / steps
            _ellipse(canvas, x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, r, r, colour)


def _outline(canvas: Canvas) -> None:
    """Ring the drawn shape with a dark line, so it reads on any background."""
    ring = {
        (x + dx, y + dy)
        for x, y in canvas
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        if (x + dx, y + dy) not in canvas
    }
    canvas.update(dict.fromkeys(ring, OUTLINE))


def _stripes(canvas: Canvas, cx: float, top: float, s: float) -> None:
    """Tabby marks: three short strokes on the forehead."""
    for dx in (-2, 0, 2):
        for dy in range(int(2 * s) + 1):
            key = (round(cx + dx * s - 0.5), round(top + dy))
            if canvas.get(key) == FUR:
                canvas[key] = STRIPE


def _ears(canvas: Canvas, cx: float, head_y: float, s: float, spread: float = 1.0) -> None:
    for side in (-1, 1):
        base = head_y - 3.5 * s
        tip = (cx + side * 5.5 * s * spread, head_y - 8.5 * s)
        outer = (cx + side * 7.5 * s * spread, base + 1.5 * s)
        inner = (cx + side * 1.8 * s * spread, base)
        _triangle(canvas, (tip, outer, inner), FUR)
        mid = (cx + side * 5 * s * spread, head_y - 6.5 * s)
        _triangle(canvas, (mid, (mid[0] + side * s, base), (mid[0] - side * 1.5 * s, base)), PINK)


def _head(canvas: Canvas, cx: float, cy: float, s: float) -> None:
    _ears(canvas, cx, cy, s)
    _ellipse(canvas, cx, cy, 7.5 * s, 5.5 * s, FUR)
    _ellipse(canvas, cx - 2.2 * s, cy + 2.6 * s, 2.6 * s, 1.9 * s, CREAM)
    _ellipse(canvas, cx + 2.2 * s, cy + 2.6 * s, 2.6 * s, 1.9 * s, CREAM)
    _stripes(canvas, cx, cy - 5.2 * s, s)


def _face(canvas: Canvas, cx: float, cy: float, s: float, eyes: str, mouth: str | None) -> None:
    """``eyes`` is open, blink or shut; ``mouth`` the colour of an open mouth, if open."""
    for side in (-1, 1):
        ex = round(cx + side * 3.2 * s - 0.5)
        ey = round(cy - 0.2 * s)
        if eyes == "open":
            for dx in (0, side):
                canvas[(ex + dx, ey)] = EYE
                canvas[(ex + dx, ey + 1)] = EYE
            canvas[(ex + (side < 0), ey + 1)] = SHUT  # a pupil, looking in
        else:
            for dx in (-1, 0, 1):
                canvas[(ex + dx, ey + 1)] = SHUT
    nose_y = round(cy + 1.4 * s)
    canvas[(round(cx - 1), nose_y)] = canvas[(round(cx), nose_y)] = PINK
    if mouth:
        canvas[(round(cx - 1), nose_y + 2)] = canvas[(round(cx), nose_y + 2)] = mouth
        canvas[(round(cx - 1), nose_y + 1)] = canvas[(round(cx), nose_y + 1)] = SHUT


def sit(canvas: Canvas, cx: float, floor: float, s: float, t: float, blink: bool) -> None:
    """Front on: a round tummy swelling with each breath, the head bobbing, the tail swaying."""
    breath = (math.sin(t * math.tau * 0.6) + 1) / 2
    cat: Canvas = {}
    sway = math.sin(t * math.tau * 0.5)
    base = (cx + 7 * s, floor - 2.0 * s)
    tail = [
        base,
        (base[0] + 4 * s, base[1] - 2 * s),
        (base[0] + 5 * s + sway * 2 * s, base[1] - 7 * s),
    ]
    tail.append((tail[-1][0] + sway * 2.5 * s - s, tail[-1][1] - 3 * s))
    _limb(cat, tail, 1.3 * s, FUR)
    _ellipse(cat, cx, floor - 6 * s, (8.5 + breath) * s, (6.5 + breath * 0.3) * s, FUR)
    _ellipse(cat, cx, floor - 5 * s, (4.5 + breath * 0.6) * s, 4.8 * s, CREAM)
    for side in (-1, 1):
        _ellipse(cat, cx + side * 3 * s, floor - 0.8 * s, 2.3 * s, 1.4 * s, CREAM)
    head_y = floor - 15 * s + breath * s
    _head(cat, cx, head_y, s)
    _outline(cat)
    _face(cat, cx, head_y, s, "blink" if blink else "open", None)
    canvas.update(cat)


def dish(canvas: Canvas, cx: float, floor: float, s: float, fill: str) -> None:
    bowl: Canvas = {}
    _ellipse(bowl, cx, floor - 1.2 * s, 5.5 * s, 2 * s, BOWL)
    _ellipse(bowl, cx, floor - 2.2 * s, 4 * s, 0.9 * s, fill)
    _outline(bowl)
    canvas.update(bowl)


def graze(canvas: Canvas, cx: float, floor: float, s: float, t: float, drinking: bool) -> None:
    """Head down in its bowl, eyes shut, jaw working (or tongue lapping) over the bowl."""
    bite = int(t * (5 if drinking else 3)) % 2
    breath = (math.sin(t * math.tau * 0.6) + 1) / 2
    cat: Canvas = {}
    _ellipse(cat, cx, floor - 9 * s, (8.5 + breath) * s, 7 * s, FUR)
    head_y = floor - 8 * s
    _head(cat, cx, head_y, s)
    _outline(cat)
    _face(cat, cx, head_y, s, "shut", (PINK if drinking else SHUT) if bite else None)
    canvas.update(cat)
    dish(canvas, cx, floor, s, (RIPPLE if bite else WATER) if drinking else KIBBLE)


def sleep(canvas: Canvas, cx: float, floor: float, s: float, t: float) -> None:
    """Curled on its side on the bed, back rising and falling, tail round its paws."""
    bed(canvas, cx, floor, s, 1.0)
    breath = (math.sin(t * math.tau * 0.3) + 1) / 2 * 1.2
    cat: Canvas = {}
    _ellipse(cat, cx + 2 * s, floor - 7.5 * s, 9.5 * s, (4.5 + breath) * s, FUR)
    for dx in (-2, 1, 4, 7):  # stripes over its back
        for dy in range(2):
            key = (round(cx + dx * s), round(floor - (11.5 + breath) * s + dy + 1))
            if cat.get(key) == FUR:
                cat[key] = STRIPE
    tail = [
        (cx + 11 * s, floor - 6 * s),
        (cx + 8 * s, floor - 3.5 * s),
        (cx - 5 * s, floor - 3.5 * s),
    ]
    _limb(cat, tail, 0.9 * s, STRIPE)  # wrapped round its paws
    head_y = floor - 7 * s
    _head(cat, cx - 7 * s, head_y, s * 0.85)
    _outline(cat)
    _face(cat, cx - 7 * s, head_y, s * 0.85, "shut", None)
    canvas.update(cat)


def bed(canvas: Canvas, cx: float, floor: float, s: float, share: float) -> None:
    """The empty bed, ``share`` of its width grown in (it pops into place under the cat)."""
    bed_: Canvas = {}
    if share > 0:
        _ellipse(bed_, cx, floor - 1.5 * s, 11.5 * s * share, 2.2 * s, BED)
        _ellipse(bed_, cx, floor - 0.6 * s, 11.5 * s * share, 1.2 * s, BED_EDGE)
        _outline(bed_)
    canvas.update(bed_)


def walk(canvas: Canvas, cx: float, floor: float, s: float, facing: int, t: float) -> None:
    """In profile, padding along: legs stepping in pairs, the round body bobbing, tail up."""
    cat: Canvas = {}
    stride = t * math.tau * 1.6
    bob = abs(math.sin(stride)) * 0.6 * s
    cy, f = floor - 8 * s - bob, facing
    sway = math.sin(stride / 2)
    tail = [(cx - f * 8 * s, cy - 2 * s), (cx - f * 11 * s, cy - 6 * s)]
    _limb(cat, [*tail, (cx - f * (10 + sway) * s, cy - 10 * s)], s, FUR)
    for hip, lead in ((5.5, 0.0), (3.0, math.pi), (-4.0, math.pi), (-6.5, 0.0)):
        swing = math.sin(stride + lead)  # diagonal pairs step together
        foot = (cx + f * (hip + swing * 2) * s, floor - 1 - max(0.0, math.cos(stride + lead)) * s)
        _limb(cat, [(cx + f * hip * s, cy + 2 * s), foot], 1.2 * s, FUR)
    _ellipse(cat, cx, cy, 9.5 * s, 5.2 * s, FUR)
    _ellipse(cat, cx + f * s, cy + 2.4 * s, 6 * s, 2.4 * s, CREAM)
    for dx in (-5, -2, 1):  # stripes over its back
        for dy in range(2):
            key = (round(cx + f * dx * s), round(cy - 4.6 * s) + dy)
            if cat.get(key) == FUR:
                cat[key] = STRIPE
    head_x, head_y = cx + f * 9.5 * s, cy - 4.5 * s
    _head(cat, head_x, head_y, s * 0.85)
    _outline(cat)
    _face(cat, head_x, head_y, s * 0.85, "open", None)
    canvas.update(cat)


__all__ = ["bed", "dish", "graze", "sit", "sleep", "walk"]
