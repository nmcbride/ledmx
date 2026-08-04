"""Progress gauges, for something else to drive.

Four styles, because "progress" is not one thing and forcing it into a
percentage bar misrepresents most of them:

``bar``     a known fraction, 0-100. The familiar case.
``blocks``  discrete steps - "5 of 12". Separated segments are easier to count
            at a glance than a smooth fill is to estimate.
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
        total = int(self.state.total or 1)
        done = int(np.clip(self.state.value, 0, total))
        # Segments with a gap between them; the gap is what makes them
        # countable rather than a single fill.
        segment = max(1, self.body_rows // max(1, total))
        thickness = max(1, segment - 1)
        for i in range(done):
            bottom = self.body_bottom - i * segment
            top = bottom - thickness
            if top < self.body_top:
                break
            out[top:bottom, :] = BAR_LEVEL
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


STYLES = ("bar", "blocks", "spin", "big")
