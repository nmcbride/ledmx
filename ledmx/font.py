"""A 3x5 bitmap font for labels and readouts.

Pillow's default font is ~11 px tall, which on a 34-row panel eats a third of
the display per line and has to be rotated to be legible at all. For short
fixed labels - "CPU", "78" - a purpose-built micro font is far better: three
characters at 3 px wide is exactly the 9-column panel width, and 5 rows leaves
plenty of room for a bar between a heading and a readout.

Glyphs are written as strings so they can be checked by eye against what they
render. Only the characters actually needed are defined; `render` skips
anything unknown rather than raising, so a stray character degrades to a gap.
"""

from __future__ import annotations

import numpy as np

GLYPH_WIDTH = 3
GLYPH_HEIGHT = 5

_GLYPHS: dict[str, tuple[str, ...]] = {
    " ": ("...", "...", "...", "...", "..."),
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", "###", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", "..#", "..#", "..#"),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
    "%": ("#.#", "..#", ".#.", "#..", "#.#"),
    "A": ("###", "#.#", "###", "#.#", "#.#"),
    "B": ("##.", "#.#", "##.", "#.#", "##."),
    "C": ("###", "#..", "#..", "#..", "###"),
    "D": ("##.", "#.#", "#.#", "#.#", "##."),
    "E": ("###", "#..", "###", "#..", "###"),
    "F": ("###", "#..", "###", "#..", "#.."),
    "G": ("###", "#..", "#.#", "#.#", "###"),
    "H": ("#.#", "#.#", "###", "#.#", "#.#"),
    "I": ("###", ".#.", ".#.", ".#.", "###"),
    "K": ("#.#", "#.#", "##.", "#.#", "#.#"),
    "L": ("#..", "#..", "#..", "#..", "###"),
    "M": ("#.#", "###", "###", "#.#", "#.#"),
    "N": ("#.#", "###", "###", "###", "#.#"),
    "O": ("###", "#.#", "#.#", "#.#", "###"),
    "P": ("###", "#.#", "###", "#..", "#.."),
    "R": ("###", "#.#", "###", "##.", "#.#"),
    "S": ("###", "#..", "###", "..#", "###"),
    "T": ("###", ".#.", ".#.", ".#.", ".#."),
    "U": ("#.#", "#.#", "#.#", "#.#", "###"),
    "W": ("#.#", "#.#", "###", "###", "#.#"),
    "X": ("#.#", "#.#", ".#.", "#.#", "#.#"),
    "Y": ("#.#", "#.#", "###", ".#.", ".#."),
    "Z": ("###", "..#", ".#.", "#..", "###"),
    "-": ("...", "...", "###", "...", "..."),
    ".": ("...", "...", "...", "...", ".#."),
    ":": ("...", ".#.", "...", ".#.", "..."),
}


def glyph(char: str) -> np.ndarray | None:
    rows = _GLYPHS.get(char.upper())
    if rows is None:
        return None
    return np.array(
        [[255 if c == "#" else 0 for c in row] for row in rows], dtype=np.uint8
    )


def text_width(text: str, *, spacing: int = 1) -> int:
    """Rendered width in pixels, including inter-character spacing."""
    n = len(text)
    if n == 0:
        return 0
    return n * GLYPH_WIDTH + (n - 1) * spacing


def render(text: str, *, spacing: int = 1, scale: int = 1) -> np.ndarray:
    """Render text as a (5*scale, width) uint8 array.

    Scaling is integer nearest-neighbour, which is the only correct choice for
    a bitmap font: every source pixel becomes an exact NxN block, so strokes
    stay uniform and edges stay hard. Any interpolating filter would blur a
    3px-wide glyph into mush.
    """
    width = text_width(text, spacing=spacing)
    out = np.zeros((GLYPH_HEIGHT, max(0, width)), dtype=np.uint8)
    x = 0
    for char in text:
        g = glyph(char)
        if g is not None:
            out[:, x:x + GLYPH_WIDTH] = g
        x += GLYPH_WIDTH + spacing

    if scale > 1:
        out = np.repeat(np.repeat(out, scale, axis=0), scale, axis=1)
    return out


def fits(text: str, width: int, *, spacing: int = 1) -> bool:
    """Does `text` fit in `width` pixels with its spacing intact?"""
    return text_width(text, spacing=spacing) <= width


def abbreviate(text: str, width: int, *, spacing: int = 1) -> str:
    """Longest prefix of `text` that fits in `width` *with* spacing.

    Glyphs are only 3 px wide, so dropping the 1 px gap between them to squeeze
    in another character is a false economy - adjacent letters run together and
    the word becomes markedly harder to read than a shorter one would be.
    """
    while text and not fits(text, width, spacing=spacing):
        text = text[:-1]
    return text


def label_for(text: str, width: int, *, spacing: int = 1) -> str:
    """A label that fits: the whole word, or failing that its initial.

    Deliberately *not* a general truncation. A partial word reads worse than a
    single letter, because the reader tries to parse it as a word - "CPU" cut
    to "CP" is merely cryptic, but "MEM" cut to "ME" is an actual English word
    pointing somewhere else entirely. One unambiguous initial beats a fragment
    that misleads.
    """
    if fits(text, width, spacing=spacing):
        return text
    return text[:1]


def draw_centered(
    target: np.ndarray, text: str, top: int, *, level: int = 255,
    spacing: int = 1, allow_tight: bool = False, scale: int = 1,
) -> None:
    """Draw `text` horizontally centred into `target` at row `top`.

    When the text is too wide, there are two ways to lose and the caller has to
    choose between them:

    ``allow_tight=False`` (default) drops trailing characters but keeps the
    spacing, so what remains stays legible. Right for labels - "CP" reads
    better than a cramped "CPU".

    ``allow_tight=True`` drops the spacing to keep every character. Required
    for numbers: truncating "100" to "10" is not a cosmetic loss, it is a wrong
    reading. Cramped and correct beats spaced and false.
    """
    height, width = target.shape
    if top < 0 or top + GLYPH_HEIGHT * scale > height:
        return

    # Spacing and fitting are measured in unscaled pixels, then scaled with
    # everything else.
    budget = width // scale
    used_spacing = spacing
    if allow_tight and not fits(text, budget, spacing=spacing):
        used_spacing = 0

    text = abbreviate(text, budget, spacing=used_spacing)
    if not text:
        return

    strip = render(text, spacing=used_spacing, scale=scale)
    x = (width - strip.shape[1]) // 2
    region = target[top:top + strip.shape[0], x:x + strip.shape[1]]
    region[strip > 0] = level
