"""Gauges, and the activity indicator that isn't one.

A **gauge** shows a value: `bar`, `blocks`, `big`. An **activity** indicator
shows only that something is happening - `spin` carries no value at all, which
is why a bar stuck at 0% is the wrong way to say "working, duration unknown".

Styles are arrangements of the primitives in `draw`, not independent
implementations. Each one decides which regions of the panel it wants and what
to put in them; the fill arithmetic, segment spacing and marker placement live
in one place. That matters because these differ far less than they appear to:
`bar` is a full-width fill, `sparkline` is nine narrow ones, `dual` is two,
`bipolar` is one anchored at the centre.

Values live in a mutable `GaugeState` so a caller can update one without the
scene being rebuilt - rebuilding restarts the animation and resets the scene
clock, making a steadily-updating gauge stutter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .. import font
from ..protocol import HEIGHT, WIDTH
from .draw import Region, block, fill, marker, segments

Size = tuple[int, int]

BAR_LEVEL = 120
TEXT_LEVEL = 255
SPIN_LEVEL = 255
PEAK_LEVEL = 185

#: Most steps drawn as separate blocks. The cap is perceptual rather than
#: physical: people read about four items instantly and can count nine or ten
#: with effort, beyond which they stop counting and start estimating - at which
#: point the blocks are decoration on a bar.
MAX_BLOCKS = 9

#: Indeterminate animations. Distinct shapes so that with several jobs running
#: the shape says which one a panel is showing.
SPINNERS = ("slide", "orbit", "chase", "wave")

#: Travel directions. Which apply depends on the spinner; `slide` bounces and
#: so ignores this entirely.
SPIN_DIRECTIONS = ("up", "down", "left", "right", "cw", "ccw")

STYLES = ("bar", "blocks", "spin", "big", "sparkline")

#: Styles that print a number under the body. The rest reclaim those rows:
#: counting blocks *is* the readout, a spinner has no value to print, and
#: `big` is nothing but its number.
HAS_READOUT = {"bar": True, "sparkline": True}


@dataclass
class GaugeState:
    """Mutable so updates do not require rebuilding the scene."""

    label: str = ""
    value: float = 0.0  # percent 0-100, or a count for big/blocks
    total: float | None = None  # set for blocks; None means a percentage
    style: str = "bar"
    spinner: str = "slide"
    #: Which way it travels: up/down for chase, left/right for wave, cw/ccw for
    #: orbit. Direction identifies a job as readily as shape does, but only for
    #: spinners that travel one way - `slide` bounces, so it ignores this.
    direction: str = "up"
    #: Recent values, for `sparkline`. Newest last.
    history: list[float] = field(default_factory=list)


class Gauge:
    """Renders a `GaugeState` onto one panel.

    The single renderer. The system monitor and caller-driven progress are the
    same picture - label, body, readout - differing only in where the number
    comes from and whether a peak marker is wanted. Two implementations meant a
    fix to one silently missed the other.

    `read` supplies a live value each frame, for gauges backed by a sensor
    rather than by something pushing updates in. `show_peak` adds a high-water
    marker, which only means anything for a value that fluctuates.
    """

    def __init__(
        self,
        state: GaugeState,
        size: Size = (HEIGHT, WIDTH),
        *,
        read: "Callable[[], float] | None" = None,
        show_peak: bool = False,
        peak_hold: float = 1.5,
        peak_fall: float = 0.22,
        history: int = WIDTH,
    ):
        self.state = state
        self.read = read
        self.show_peak = show_peak
        self.peak_hold = peak_hold
        self.peak_fall = peak_fall
        self.history = history

        self._peak = 0.0
        self._peak_at = 0.0
        self._last_t = 0.0

        self.h, self.w = size
        self.label_rows = font.GLYPH_HEIGHT

    # -- regions -----------------------------------------------------------

    def body(self, *, readout: bool) -> Region:
        """Rows available for the body, given whether a readout is drawn."""
        top = self.label_rows + 1
        bottom = self.h - (font.GLYPH_HEIGHT + 1) if readout else self.h
        return Region(top, bottom)

    # -- styles ------------------------------------------------------------

    def _bar(self, out: np.ndarray, region: Region, t: float) -> str:
        fraction = float(np.clip(self.state.value / 100.0, 0.0, 1.0))
        filled = fill(out, region, fraction, level=BAR_LEVEL)
        if self.show_peak:
            self._peak_marker(out, region, fraction, filled, t)
        return str(int(round(fraction * 100)))

    def _peak_marker(self, out, region: Region, fraction: float, filled: int,
                     t: float) -> None:
        """High-water mark that holds, then slides down to meet the bar.

        Tracks the value the bar draws rather than a raw sample, so it can
        never sit below the bar or run ahead of it. Snapping back reads as a
        glitch; sliding reads as a measurement decaying, which is what it is.
        """
        dt = max(0.0, min(0.5, t - self._last_t))
        if fraction >= self._peak:
            self._peak, self._peak_at = fraction, t
        elif t - self._peak_at > self.peak_hold:
            self._peak = max(fraction, self._peak - self.peak_fall * dt)

        row = region.bottom - 1 - int(round(self._peak * (region.rows - 1)))
        if region.top <= row < region.bottom - filled:
            marker(out, region, self._peak, level=PEAK_LEVEL)

    def _blocks(self, out: np.ndarray, region: Region) -> str:
        """One block per step, up to MAX_BLOCKS.

        Never folds several steps into one block. Folding breaks the only
        property this style has over `bar` - that a block *is* a step - and
        then needs clamping at both ends to stop "nearly done" rendering as
        "done". Callers asking for more steps than fit get clamped instead.
        """
        count = max(1, min(int(self.state.total or 1), MAX_BLOCKS))
        lit = int(np.clip(self.state.value, 0, count))
        segments(out, region, count, lit, level=BAR_LEVEL)
        return ""

    def _sparkline(self, out: np.ndarray, region: Region) -> str:
        """Recent samples as narrow bars - shows trend, not just level.

        The only gauge here that answers "where has this been" rather than
        "where is it now". A bar at 60% says nothing about whether it was 30%
        a minute ago.
        """
        samples = self.state.history[-self.history:]
        if not samples:
            return ""
        columns = region.split(self.history, 0, self.w)
        # Right-align, so the newest sample is always in the same place and the
        # chart grows leftward rather than sliding.
        for col, value in zip(columns[-len(samples):], samples):
            fill(out, col, float(value) / 100.0, level=BAR_LEVEL)
        return str(int(round(samples[-1])))

    def _spin(self, out: np.ndarray, region: Region, t: float) -> str:
        """Indeterminate animation. Carries no value - it is not a gauge.

        Several shapes for the same reason the alerts have several: with more
        than one job running, the shape says which one this panel is showing
        without having to read the label.
        """
        kind = self.state.spinner
        reverse = self.state.direction in ("down", "left", "ccw")

        if kind == "orbit":
            self._orbit(out, region, t, reverse)
        elif kind == "chase":
            # Three blocks in convoy, one direction, wrapping - reads as steady
            # throughput rather than as something oscillating.
            height = max(2, region.rows // 8)
            for i in range(3):
                pos = (t * 0.45 + i / 3.0) % 1.0
                block(out, region, pos if reverse else 1.0 - pos, height,
                      level=SPIN_LEVEL)
        elif kind == "wave":
            # The only shape using the width, so it looks nothing like the
            # others. Direction sets which way the phase travels.
            for i, col in enumerate(region.split(self.w, 0, self.w)):
                src = (self.w - 1 - i) if reverse else i
                level = 0.5 - 0.5 * np.cos(t * 4.0 + src * 0.7)
                fill(out, col, level, level=SPIN_LEVEL)
        else:
            # slide: one block, bouncing. Direction is ignored - it reverses at
            # both ends, so within a second there is nothing left to say which
            # way it set off.
            travel = (t * 0.9) % 2.0
            phase = travel if travel < 1.0 else 2.0 - travel
            block(out, region, phase, max(3, region.rows // 4), level=SPIN_LEVEL)
        return ""

    def _orbit(self, out: np.ndarray, region: Region, t: float,
               reverse: bool) -> None:
        """A block travelling the perimeter - the only shape using the edges."""
        size = 2
        # Walk the path of the block's top-left corner, which stops short of
        # the far edges. Walking the full perimeter instead pushes half the
        # block outside the panel along the bottom and right, so it appears to
        # shrink on two sides of every lap.
        max_x, max_y = self.w - size, region.rows - size
        perimeter = 2 * max_x + 2 * max_y
        pos = (t * 16) % perimeter
        if reverse:
            pos = perimeter - pos

        if pos < max_x:
            x, y = pos, 0
        elif pos < max_x + max_y:
            x, y = max_x, pos - max_x
        elif pos < 2 * max_x + max_y:
            x, y = 2 * max_x + max_y - pos, max_y
        else:
            x, y = 0, perimeter - pos

        x, y = int(round(x)), int(round(y))
        out[region.top + y:region.top + y + size, x:x + size] = SPIN_LEVEL

    def _big(self, out: np.ndarray) -> None:
        """A bare count. No bar, because there is nothing to be a fraction of."""
        text = str(int(self.state.value))
        scale = 2 if font.fits(text, self.w // 2) else 1
        top = max(0, (self.h - font.GLYPH_HEIGHT * scale) // 2)
        font.draw_centered(out, text, top, level=TEXT_LEVEL, scale=scale,
                           allow_tight=True)

    # -- render ------------------------------------------------------------

    def __call__(self, t: float) -> np.ndarray:
        if self.read is not None:
            self.state.value = self.read()

        out = np.zeros((self.h, self.w), dtype=np.uint8)
        style = self.state.style
        region = self.body(readout=HAS_READOUT.get(style, False))

        if style == "big":
            self._big(out)
            readout = ""
        elif style == "blocks":
            readout = self._blocks(out, region)
        elif style == "spin":
            readout = self._spin(out, region, t)
        elif style == "sparkline":
            readout = self._sparkline(out, region)
        else:
            readout = self._bar(out, region, t)

        font.draw_centered(
            out, font.label_for(self.state.label, self.w), 0, level=TEXT_LEVEL
        )
        if readout:
            font.draw_centered(
                out, readout, self.h - font.GLYPH_HEIGHT,
                level=TEXT_LEVEL, allow_tight=True,
            )
        self._last_t = t
        return out
