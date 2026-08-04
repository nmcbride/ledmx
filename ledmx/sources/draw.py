"""Drawing primitives shared by every gauge style.

Each style is a different arrangement of the same few operations: fill part of
a region proportionally, cut it into segments, mark a position. `bar` fills the
full width from the bottom, `sparkline` is nine of those side by side, `dual` is
two, `bipolar` is one anchored at the centre, `blocks` is a fill with gaps
carved out of it.

Keeping those operations here rather than inside each style is what stops the
same fill arithmetic being written five times - the mistake that produced two
independent gauge implementations, where fixing the peak marker in one silently
missed the other.

A `Region` is a half-open row range: rows `top` up to but not including
`bottom`, matching numpy slicing.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Region:
    """A half-open range of rows, with an optional column range."""

    top: int
    bottom: int
    left: int = 0
    right: int | None = None

    @property
    def rows(self) -> int:
        return max(0, self.bottom - self.top)

    def columns(self, width: int) -> slice:
        return slice(self.left, width if self.right is None else self.right)

    def inset(self, *, top: int = 0, bottom: int = 0) -> "Region":
        return Region(self.top + top, self.bottom - bottom, self.left, self.right)

    def split(self, parts: int, gap: int, width: int) -> list["Region"]:
        """Divide horizontally into `parts` columns separated by `gap`.

        Used by anything showing several values side by side - a sparkline's
        samples, a dual gauge's pair. The remainder goes to the leftmost
        columns rather than being dropped, so the parts together span the full
        width.
        """
        right = width if self.right is None else self.right
        span = right - self.left
        usable = span - gap * (parts - 1)
        if usable < parts:
            return [Region(self.top, self.bottom, self.left, right)]

        base, extra = divmod(usable, parts)
        out: list[Region] = []
        x = self.left
        for i in range(parts):
            w = base + (1 if i < extra else 0)
            out.append(Region(self.top, self.bottom, x, x + w))
            x += w + gap
        return out


def fill(
    out: np.ndarray,
    region: Region,
    fraction: float,
    *,
    level: int,
    anchor: str = "bottom",
) -> int:
    """Fill `fraction` of `region`, growing from `anchor`. Returns rows filled.

    ``bottom`` and ``top`` grow from that edge; ``centre`` grows both ways from
    the middle, which is what a signed value needs - a bottom-anchored bar
    cannot show negative at all.
    """
    fraction = float(np.clip(fraction, -1.0, 1.0))
    cols = region.columns(out.shape[1])

    if anchor == "centre":
        middle = (region.top + region.bottom) // 2
        extent = int(round(abs(fraction) * (region.rows / 2)))
        if extent == 0:
            return 0
        if fraction >= 0:
            out[max(region.top, middle - extent):middle, cols] = level
        else:
            out[middle:min(region.bottom, middle + extent), cols] = level
        return extent

    filled = int(round(abs(fraction) * region.rows))
    if filled <= 0:
        return 0
    if anchor == "top":
        out[region.top:region.top + filled, cols] = level
    else:
        out[region.bottom - filled:region.bottom, cols] = level
    return filled


def segments(
    out: np.ndarray,
    region: Region,
    count: int,
    lit: int,
    *,
    level: int,
    gap: int = 1,
) -> None:
    """Draw `lit` of `count` equal blocks, bottom up, separated by `gap` rows.

    Every block is the same height and every gap the same size; any remainder
    is left dark at the top. Distributing it into some of the gaps instead
    makes the spacing look meaningful when it is only rounding, and unequal
    blocks read as though a larger one mattered more.
    """
    count = max(1, count)
    cols = region.columns(out.shape[1])
    gaps = gap * (count - 1)
    block = max(1, (region.rows - gaps) // count)

    bottom = region.bottom
    for _ in range(max(0, lit)):
        top = bottom - block
        if top < region.top:
            break
        out[top:bottom, cols] = level
        bottom = top - gap


def marker(
    out: np.ndarray, region: Region, fraction: float, *, level: int
) -> int:
    """Draw a one-row marker at `fraction` up `region`. Returns its row."""
    fraction = float(np.clip(fraction, 0.0, 1.0))
    row = region.bottom - 1 - int(round(fraction * (region.rows - 1)))
    if region.top <= row < region.bottom:
        out[row, region.columns(out.shape[1])] = level
    return row


def block(
    out: np.ndarray, region: Region, position: float, height: int, *, level: int
) -> None:
    """Draw a solid block of `height` rows at `position` (0.0 top, 1.0 bottom)."""
    travel = max(0, region.rows - height)
    top = region.top + int(round(float(np.clip(position, 0.0, 1.0)) * travel))
    out[top:top + height, region.columns(out.shape[1])] = level
