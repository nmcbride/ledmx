"""Progress gauges, for something else to drive.

Four styles, because "progress" is not one thing and forcing it into a
percentage bar misrepresents most of them:

``bar``     a known fraction, 0-100. The familiar case.
``blocks``  discrete steps - "3 of 5". Right when the number says *which*
            stage you are in rather than how far along: a deploy pipeline, a
            retry count, named phases. Use it when you would say the value out
            loud as "step 3 of 5"; use ``bar`` when you would say "62%".
``spin``    unknown duration. An indeterminate animation, because a bar stuck
            at 0% reads as broken rather than as busy, and "working, no idea
            how long" is the honest state for a great many tasks.
``big``     a count rather than a fraction - 42 tests, 7 errors. No bar at all,
            since there is nothing to be a fraction of.

Values live in a mutable `GaugeState` so a caller can update one without the
scene being rebuilt - rebuilding would restart the animation and reset the
scene clock, making a steadily-updating gauge stutter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .. import font
from ..protocol import HEIGHT, WIDTH

Size = tuple[int, int]

BAR_LEVEL = 120
TEXT_LEVEL = 255
SPIN_LEVEL = 255
PEAK_LEVEL = 185


@dataclass
class GaugeState:
    """Mutable so updates do not require rebuilding the scene."""

    label: str = ""
    value: float = 0.0  # percent, 0-100 (or a count for "big"/"blocks")
    total: float | None = None  # set for "blocks"; None means a percentage
    style: str = "bar"


class Gauge:
    """Renders a `GaugeState` onto one panel.

    The single gauge renderer. The system monitor and caller-driven progress
    are the same picture - a label, a fill, a readout - differing only in where
    the number comes from and whether a peak marker is wanted. Keeping two
    implementations meant a fix to one silently missed the other.

    `read` supplies a live value each frame, for gauges backed by a sensor
    rather than by something pushing updates. `show_peak` adds a high-water
    marker, which only makes sense for a value that fluctuates.
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
    ):
        self.state = state
        self.read = read
        self.show_peak = show_peak
        self.peak_hold = peak_hold
        self.peak_fall = peak_fall
        self._peak = 0.0
        self._peak_at = 0.0
        self._last_t = 0.0

        self.h, self.w = size
        self.body_top = font.GLYPH_HEIGHT + 1
        self.body_bottom = self.h - font.GLYPH_HEIGHT - 1
        self.body_rows = max(1, self.body_bottom - self.body_top)

    def _draw_peak(self, out: np.ndarray, fraction: float, filled: int,
                   t: float) -> None:
        """High-water marker that holds, then slides down to meet the bar.

        Tracks the same value the bar draws rather than a raw sample, so the
        marker can never sit below the bar or run ahead of it. Snapping back
        instead of sliding reads as a glitch; descending reads as a
        measurement decaying, which is what it is.
        """
        dt = max(0.0, min(0.5, t - self._last_t))
        if fraction >= self._peak:
            self._peak = fraction
            self._peak_at = t
        elif t - self._peak_at > self.peak_hold:
            self._peak = max(fraction, self._peak - self.peak_fall * dt)

        row = self.body_bottom - 1 - int(round(self._peak * (self.body_rows - 1)))
        if self.body_top <= row < self.body_bottom - filled:
            out[row, :] = PEAK_LEVEL

    # -- styles ------------------------------------------------------------

    def _draw_bar(self, out: np.ndarray, t: float) -> str:
        fraction = float(np.clip(self.state.value / 100.0, 0.0, 1.0))
        filled = int(round(fraction * self.body_rows))
        if filled:
            out[self.body_bottom - filled:self.body_bottom, :] = BAR_LEVEL
        if self.show_peak:
            self._draw_peak(out, fraction, filled, t)
        return str(int(round(fraction * 100)))

    def _draw_blocks(self, out: np.ndarray) -> str:
        """Discrete countable segments, with larger step counts folded to fit.

        Works at any total: steps beyond MAX_BLOCKS are folded so each drawn
        segment stands for several of them, rather than the style degrading to
        a plain fill exactly when there are enough steps to be worth counting.

        The readout always reports the real count. Folding is a display
        constraint and should not change the number a caller is told.
        """
        total = max(1, int(self.state.total or 1))
        done = int(np.clip(self.state.value, 0, total))

        # Fold the steps onto however many segments the panel can actually
        # separate. A countable segment needs a lit row plus a blank row, so
        # 22 body rows cap it at MAX_BLOCKS; beyond that each drawn segment
        # stands for several steps. Dropping to a plain fill instead would
        # abandon the one thing this style is for - being countable - exactly
        # when the step count is high enough to be worth counting.
        drawn = min(total, MAX_BLOCKS)
        lit = int(round(done / total * drawn))
        # Clamp both ends so the two states that matter most stay unambiguous.
        # Any progress lights at least one segment, so "started" never looks
        # like "not started"; anything short of complete leaves one unlit, so
        # "nearly done" never looks like "done". Without the upper clamp,
        # folding 40 steps onto 11 segments renders 39 and 40 identically -
        # and being told a job has finished when it has not is the worse of
        # the two errors.
        if done > 0:
            lit = max(1, lit)
        if done < total:
            lit = min(lit, drawn - 1)

        # Segment height stays fractional and is rounded per edge, so the stack
        # spans the whole body. Integer division discards the remainder: 22
        # rows over 8 steps gives 2 rows each, filling 16 and leaving 6
        # permanently dark, so a finished task displays as two thirds complete.
        segment = self.body_rows / drawn

        # Fill the exact fraction, then carve gaps at the internal boundaries.
        # Giving each segment its own gap costs a row at whichever end the gap
        # sits, so the stack could never reach the top; only the boundaries
        # *between* segments need separating.
        top = max(self.body_top, int(round(self.body_bottom - lit * segment)))
        if top < self.body_bottom:
            out[top:self.body_bottom, :] = BAR_LEVEL
        for i in range(1, lit):
            row = int(round(self.body_bottom - i * segment))
            if self.body_top <= row < self.body_bottom:
                out[row, :] = 0

        # Readout is the real count, not the segment count - the folding is a
        # display constraint and should not change the number reported.
        return str(done)

    def _draw_spin(self, out: np.ndarray, t: float) -> str:
        """A block sliding up and back. Deliberately never completes."""
        travel = (t * 0.9) % 2.0
        phase = travel if travel < 1.0 else 2.0 - travel
        height = max(3, self.body_rows // 4)
        top = self.body_top + int(phase * (self.body_rows - height))
        out[top:top + height, :] = SPIN_LEVEL
        return ""

    def _draw_big(self, out: np.ndarray) -> str:
        return str(int(self.state.value))

    # -- render ------------------------------------------------------------

    def __call__(self, t: float) -> np.ndarray:
        if self.read is not None:
            self.state.value = self.read()
        out = np.zeros((self.h, self.w), dtype=np.uint8)
        style = self.state.style

        if style == "big":
            # No bar: a count has nothing to be a fraction of, so the number
            # gets the whole panel and is drawn as large as will fit.
            text = self._draw_big(out)
            scale = 2 if font.fits(text, self.w // 2) else 1
            top = max(0, (self.h - font.GLYPH_HEIGHT * scale) // 2)
            font.draw_centered(out, text, top, level=TEXT_LEVEL,
                               scale=scale, allow_tight=True)
            font.draw_centered(
                out, font.label_for(self.state.label, self.w), 0,
                level=TEXT_LEVEL,
            )
            return out

        if style == "blocks":
            readout = self._draw_blocks(out)
        elif style == "spin":
            readout = self._draw_spin(out, t)
        else:
            readout = self._draw_bar(out, t)

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


#: Most steps drawn as separate segments. The cap is perceptual, not physical:
#: a few more would fit, but people read about four items instantly and can
#: count eight or ten with effort - beyond that they stop counting and start
#: estimating proportion, at which point the segments have stopped doing their
#: job. Fewer segments also means chunkier ones, which are easier to tell apart.
MAX_BLOCKS = 10

STYLES = ("bar", "blocks", "spin", "big")
