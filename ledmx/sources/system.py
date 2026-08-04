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

from ..protocol import HEIGHT, WIDTH
from .progress import Gauge, GaugeState

Size = tuple[int, int]



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


def build_gauges(names: list[str], size: Size = (HEIGHT, WIDTH)) -> tuple:
    """One gauge per panel: CPU first, then memory, cycling if there are more.

    Uses the same `Gauge` renderer that caller-driven progress does. These are
    the same picture - label, fill, readout - and differ only in where the
    number comes from: a `read` callable pulling from /proc rather than
    something pushing updates in.
    """
    stats = SystemStats()
    metrics = [
        # Values are percentages, matching what the gauge and the CLI expect.
        ("CPU", lambda: stats.cpu * 100.0, True),
        ("MEM", lambda: stats.memory * 100.0, False),
    ]
    gauges = {}
    for i, name in enumerate(names):
        label, read, peak = metrics[i % len(metrics)]
        gauges[name] = Gauge(
            GaugeState(label=label, style="bar"), size,
            read=read, show_peak=peak,
        )
    return stats, gauges
