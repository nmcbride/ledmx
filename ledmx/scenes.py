"""Named scenes the daemon can switch between.

A scene builds a *producer*: a callable taking a timestamp and returning one
frame per panel. Two shapes exist, because the two kinds of content want
different things from the panel arrangement:

**Canvas scenes** render once at the layout's virtual size and let the layout
slice per-panel frames out of it. Right for effects and video, which look best
spanning whatever display the panels currently form.

**Per-panel scenes** build an independent source for each panel. Right for the
monitor, where each panel is a self-contained gauge - that way it reads
correctly whether the modules sit adjacent or flank the keyboard, with no
content falling into a gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

import numpy as np

from . import canvas as canvas_mod
from .layout import Layout
from .protocol import HEIGHT, WIDTH
from .sources.procedural import Fire, Life, MatrixRain, Plasma, Sweep
from .sources.clock import build_clock
from .sources.system import build_gauges

Producer = Callable[[float], Mapping[str, np.ndarray]]


@dataclass(frozen=True)
class Scene:
    name: str
    build: Callable[[Layout, list[str]], Producer]
    description: str = ""


def _canvas_scene(factory, *, gamma: float = 2.2):
    """Render at layout size, slice per panel."""

    def build(layout: Layout, names: list[str]) -> Producer:
        source = factory(layout.size)

        def produce(t: float):
            frame = source(t)
            if gamma:
                frame = canvas_mod.gamma(frame, gamma)
            return layout.slice(frame)

        return produce

    return build


def _clock(layout: Layout, names: list[str]) -> Producer:
    # Splitting hours from minutes only reads as a time when the panels touch.
    panels = build_clock(names, (HEIGHT, WIDTH), contiguous=layout.contiguous)

    def produce(t: float):
        return {name: panels[name](t) for name in names}

    return produce


def _monitor(layout: Layout, names: list[str]) -> Producer:
    stats, gauges = build_gauges(names, (HEIGHT, WIDTH))

    def produce(t: float):
        stats.update(t)
        return {name: gauges[name](t) for name in names}

    return produce


def _blank(layout: Layout, names: list[str]) -> Producer:
    dark = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)

    def produce(t: float):
        return {name: dark for name in names}

    return produce


SCENES: dict[str, Scene] = {
    "clock": Scene("clock", _clock, "hours on one panel, minutes on the next"),
    "monitor": Scene("monitor", _monitor, "CPU and memory gauges, one per panel"),
    "rain": Scene("rain", _canvas_scene(MatrixRain), "falling drops with trails"),
    "plasma": Scene("plasma", _canvas_scene(Plasma), "smooth interference pattern"),
    "fire": Scene("fire", _canvas_scene(Fire), "upward-propagating fire"),
    "life": Scene("life", _canvas_scene(Life), "Conway's Game of Life"),
    "sweep": Scene("sweep", _canvas_scene(Sweep), "a moving bar; checks geometry"),
    "off": Scene("off", _blank, "panels blank, daemon still running"),
}

#: Order used by `next` / `prev`. "off" is deliberately excluded so cycling
#: through scenes with a hotkey never lands on a blank display by accident;
#: reach it explicitly instead.
CYCLE = ["monitor", "clock", "rain", "plasma", "fire", "life"]


def get(name: str) -> Scene:
    if name not in SCENES:
        raise KeyError(
            f"unknown scene '{name}'; known: {', '.join(sorted(SCENES))}"
        )
    return SCENES[name]


def next_in_cycle(current: str, step: int = 1) -> str:
    if current in CYCLE:
        return CYCLE[(CYCLE.index(current) + step) % len(CYCLE)]
    return CYCLE[0]
