"""Frame pacing and multi-panel dispatch.

Measured facts that drive this design:

* The firmware parses one command per main-loop iteration, and services roughly
  59 commands per second. A greyscale frame costs 10 commands (9 columns plus a
  flush), a 1-bit frame costs 1.
* The two panels sit on separate internal hubs and parallelise at about 74%
  efficiency, so one writer thread per panel is worth roughly 1.5x over writing
  them in sequence.

Two display modes follow from that:

**Greyscale** - the writer waits for new frames and sends the latest, skipping
unchanged columns. Full depth, ~6 fps, right for content that is mostly static.

**Dithered 1-bit** - the writer *free-runs*, continuously re-thresholding the
most recent frame with an advancing dither phase. Sub-frames must keep flowing
even when content is still, because the perceived brightness of a pixel is its
duty cycle over time. ~59 sub-frames per second, so content can update at 20-30
fps and still look smooth.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

import numpy as np

from .canvas import Frame
from .device import Panel

Producer = Callable[[float], Mapping[str, Frame]]


class _PanelWriter(threading.Thread):
    """Drives one panel. Either frame-driven (greyscale) or free-running (1-bit)."""

    def __init__(self, name: str, panel: Panel, ditherer=None):
        super().__init__(name=f"ledmx-{name}", daemon=True)
        self.panel = panel
        #: Swappable at runtime: a panel's render mode follows whichever scene
        #: is currently assigned to it, and that can change under a hotkey.
        self.ditherer = ditherer
        self._pending: Frame | None = None
        self._latest: Frame | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.written = 0
        self.dropped = 0

    def submit(self, frame: Frame) -> None:
        with self._lock:
            if self.ditherer is None and self._pending is not None:
                self.dropped += 1
            self._pending = frame
            self._latest = frame
        self._wake.set()

    def set_ditherer(self, ditherer) -> None:
        """Switch render mode. None means native greyscale."""
        with self._lock:
            self.ditherer = ditherer

    def run(self) -> None:
        phase = 0
        while not self._stop.is_set():
            with self._lock:
                ditherer = self.ditherer
                if ditherer is None:
                    frame, self._pending = self._pending, None
                else:
                    frame = self._latest

            if frame is None:
                # Nothing pending; wait briefly rather than spinning.
                if not self._wake.wait(timeout=0.05):
                    continue
                self._wake.clear()
                continue

            try:
                if ditherer is None:
                    self.panel.show(frame)
                else:
                    self.panel.show_bw(ditherer(frame, phase))
                    phase += 1
                self.written += 1
            except OSError:
                break
            if ditherer is None:
                self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


@dataclass
class Stats:
    frames: int = 0
    elapsed: float = 0.0
    per_panel_written: dict[str, int] = field(default_factory=dict)
    per_panel_dropped: dict[str, int] = field(default_factory=dict)

    @property
    def fps(self) -> float:
        return self.frames / self.elapsed if self.elapsed else 0.0


class Runner:
    """Drives a producer at a target content rate across one or more panels."""

    def __init__(
        self,
        panels: Mapping[str, Panel],
        *,
        fps: float = 30.0,
        brightness: int | None = None,
        ditherer=None,
    ):
        self.panels = dict(panels)
        self.fps = fps
        self.ditherer = ditherer
        if brightness is not None:
            for panel in self.panels.values():
                panel.set_brightness(brightness)
        for panel in self.panels.values():
            panel.set_sleeping(False)

    def run(
        self,
        producer: Producer,
        *,
        duration: float | None = None,
        clear_on_exit: bool = True,
    ) -> Stats:
        writers = {
            name: _PanelWriter(name, panel, self.ditherer)
            for name, panel in self.panels.items()
        }
        for w in writers.values():
            w.start()

        interval = 1.0 / self.fps
        start = time.perf_counter()
        next_deadline = start
        frames = 0

        try:
            while True:
                now = time.perf_counter()
                t = now - start
                if duration is not None and t >= duration:
                    break

                produced = producer(t)
                if produced is None:
                    break

                for name, frame in produced.items():
                    writer = writers.get(name)
                    if writer is not None:
                        writer.submit(frame)
                frames += 1

                next_deadline += interval
                slack = next_deadline - time.perf_counter()
                if slack > 0:
                    time.sleep(slack)
                else:
                    next_deadline = time.perf_counter()
        except KeyboardInterrupt:
            pass
        finally:
            elapsed = time.perf_counter() - start
            for w in writers.values():
                w.stop()
            for w in writers.values():
                w.join(timeout=1.0)
            if clear_on_exit:
                for panel in self.panels.values():
                    try:
                        panel.clear()
                    except OSError:
                        pass

        return Stats(
            frames=frames,
            elapsed=elapsed,
            per_panel_written={n: w.written for n, w in writers.items()},
            per_panel_dropped={n: w.dropped for n, w in writers.items()},
        )


def mirror(frame_fn: Callable[[float], Frame], names: list[str]) -> Producer:
    def produce(t: float) -> Mapping[str, Frame]:
        frame = frame_fn(t)
        return {name: frame for name in names}

    return produce


def independent(sources: Mapping[str, Callable[[float], Frame]]) -> Producer:
    def produce(t: float) -> Mapping[str, Frame]:
        return {name: fn(t) for name, fn in sources.items()}

    return produce
