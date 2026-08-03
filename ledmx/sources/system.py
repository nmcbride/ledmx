"""System monitor: one labelled gauge per panel.

Reads /proc directly - no dependencies beyond the standard library.

Each panel shows a single metric as a self-contained 9x34 gauge:

    rows 0-4    label      "CPU" / "MEM", 3x5 micro font
    rows 6-27   bar        fills from the bottom, 22 rows of resolution
    rows 29-33  readout    percentage, same micro font

Building it per panel rather than as one wide canvas means the display works
identically whether the panels sit adjacent or flank the keyboard - there is no
spanning content to fall into a gap. Adding a third panel would just mean a
third gauge.

**Legibility beats information density.** An earlier version drew one
one-pixel-wide bar per hardware thread, which fit the canvas exactly and was
unreadable - at this size a single-pixel bar reads as noise. The display has
306 pixels; a few large shapes and a couple of words is what it can carry.
"""

from __future__ import annotations

import numpy as np

from .. import font
from ..protocol import HEIGHT, WIDTH

Size = tuple[int, int]

BAR_LEVEL = 110
TEXT_LEVEL = 255
PEAK_LEVEL = 180


def _read_cpu_times() -> tuple[int, int]:
    """Aggregate (busy, total) jiffies from /proc/stat."""
    with open("/proc/stat", "r") as fh:
        parts = fh.readline().split()
    values = [int(v) for v in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total - idle, total


def _read_memory() -> float:
    """Fraction of memory in use, 0.0-1.0."""
    total = available = None
    with open("/proc/meminfo", "r") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                total = float(line.split()[1])
            elif line.startswith("MemAvailable:"):
                available = float(line.split()[1])
            if total is not None and available is not None:
                break
    if not total:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (available or 0.0) / total))


class SystemStats:
    """Samples /proc and exposes smoothed CPU and memory fractions.

    `interval` is how often /proc is re-read. CPU utilisation is a *delta*
    between samples, so sampling as fast as the display updates would measure
    intervals too short to be meaningful and the bar would jitter. Sampling
    slower and holding between samples gives a steadier, more readable bar.
    """

    def __init__(self, *, interval: float = 0.5, smoothing: float = 0.5):
        self.interval = interval
        self.smoothing = smoothing
        self._prev = _read_cpu_times()
        self._last = 0.0
        self.cpu = 0.0
        self.memory = _read_memory()

    def update(self, t: float) -> None:
        if t - self._last < self.interval:
            return
        busy, total = _read_cpu_times()
        prev_busy, prev_total = self._prev
        d_total = total - prev_total
        raw = (busy - prev_busy) / d_total if d_total > 0 else 0.0
        self._prev = (busy, total)

        raw = max(0.0, min(1.0, raw))
        a = self.smoothing
        self.cpu = a * self.cpu + (1.0 - a) * raw
        self.memory = _read_memory()
        self._last = t


class Gauge:
    """A labelled bar with a percentage readout, sized for one panel."""

    def __init__(
        self,
        label: str,
        read: "callable[[], float]",
        size: Size = (HEIGHT, WIDTH),
        *,
        peak_hold: float = 2.5,
        show_peak: bool = True,
    ):
        self.h, self.w = size
        self.label = label
        self.read = read
        self.peak_hold = peak_hold
        self.show_peak = show_peak

        self._peak = 0.0
        self._peak_at = 0.0

        # Bar occupies the space between the label and the readout, with a
        # blank row either side so the text never touches the fill.
        self.bar_top = font.GLYPH_HEIGHT + 1
        self.bar_bottom = self.h - font.GLYPH_HEIGHT - 1
        self.bar_rows = max(1, self.bar_bottom - self.bar_top)

    def __call__(self, t: float) -> np.ndarray:
        value = max(0.0, min(1.0, self.read()))
        out = np.zeros((self.h, self.w), dtype=np.uint8)

        filled = int(round(value * self.bar_rows))
        if filled > 0:
            out[self.bar_bottom - filled:self.bar_bottom, :] = BAR_LEVEL

        if self.show_peak:
            if value >= self._peak:
                self._peak = value
                self._peak_at = t
            elif t - self._peak_at > self.peak_hold:
                self._peak = value
                self._peak_at = t
            # Peak tracks the same smoothed value the bar draws, so the marker
            # can never sit below the bar or run ahead of it.
            peak_row = self.bar_bottom - 1 - int(round(self._peak * (self.bar_rows - 1)))
            if self.bar_top <= peak_row < self.bar_bottom - filled:
                out[peak_row, :] = PEAK_LEVEL

        # Label falls back to a single initial rather than a word fragment;
        # the reading never truncates, it tightens, so 100 stays "100".
        font.draw_centered(
            out, font.label_for(self.label, self.w), 0, level=TEXT_LEVEL
        )
        font.draw_centered(
            out, str(int(round(value * 100))), self.h - font.GLYPH_HEIGHT,
            level=TEXT_LEVEL, allow_tight=True,
        )
        return out


def build_gauges(names: list[str], size: Size = (HEIGHT, WIDTH)) -> dict:
    """One gauge per panel: CPU first, then memory, cycling if there are more."""
    stats = SystemStats()
    metrics = [
        ("CPU", lambda: stats.cpu),
        ("MEM", lambda: stats.memory),
    ]
    gauges = {}
    for i, name in enumerate(names):
        label, read = metrics[i % len(metrics)]
        gauges[name] = Gauge(label, read, size, show_peak=(label == "CPU"))
    return stats, gauges
