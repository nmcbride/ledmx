"""Wordless alert patterns.

A scrolling message needs you to read it; an alert only needs you to recognise
it. That is a much lower bar from across a room, and it is what most
notifications actually want - you rarely need the text of "build finished", you
need to know which of the three things you are waiting on just happened.

Monochrome 9x34 leaves two axes that a person can reliably tell apart at a
glance: **direction** and **rhythm**. Colour is unavailable and fine detail does
not survive the resolution, so the set below varies only those, and leans on
existing associations - rising for success, falling for failure - so the
mapping does not have to be memorised.

Each alert declares its own render mode. Motion patterns run 1-bit at high
frame rate, because a bar crossing 34 rows at 6 fps moves in visible jumps;
`pulse` runs greyscale, because a smooth brightness ramp is the whole point of
it and 1-bit cannot express one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

import numpy as np

from .protocol import HEIGHT, WIDTH

Frame = np.ndarray
Producer = Callable[[float], Mapping[str, Frame]]


@dataclass(frozen=True)
class Alert:
    name: str
    render: Callable[[float, float], Frame]  # (t, duration) -> frame
    duration: float
    description: str
    mode: str = "bw"
    fps: float = 50.0


def _blank() -> Frame:
    return np.zeros((HEIGHT, WIDTH), dtype=np.uint8)


def _bar(centre: float, thickness: int = 4, level: int = 255) -> Frame:
    out = _blank()
    top = int(round(centre - thickness / 2))
    for i in range(thickness):
        y = top + i
        if 0 <= y < HEIGHT:
            out[y, :] = level
    return out


def _rise(t: float, duration: float) -> Frame:
    """A bar sweeping upward. Reads as completion, success, arrival."""
    phase = min(1.0, t / duration)
    return _bar(HEIGHT - phase * (HEIGHT + 4) + 2)


def _fall(t: float, duration: float) -> Frame:
    """A bar sweeping downward. Reads as failure, stop, departure."""
    phase = min(1.0, t / duration)
    return _bar(phase * (HEIGHT + 4) - 2)


def _blink(t: float, duration: float) -> Frame:
    """Hard on/off flashes. Deliberately the most attention-grabbing."""
    out = _blank()
    if int(t * 6) % 2 == 0:
        out[:, :] = 255
    return out


def _pulse(t: float, duration: float) -> Frame:
    """A smooth brightness swell. Calm - for things that are not urgent."""
    out = _blank()
    # Two full breaths over the duration, never fully dark so it reads as a
    # swell rather than as flashing.
    level = 0.15 + 0.85 * (0.5 - 0.5 * np.cos(2 * np.pi * 2 * t / duration))
    out[:, :] = int(np.clip(level, 0, 1) * 255)
    return out


def _fill(t: float, duration: float) -> Frame:
    """Fills from the bottom, then empties. Reads as a task completing."""
    out = _blank()
    phase = t / duration
    level = phase * 2 if phase < 0.5 else (1.0 - phase) * 2
    filled = int(round(np.clip(level, 0, 1) * HEIGHT))
    if filled:
        out[HEIGHT - filled:, :] = 255
    return out


def _converge(t: float, duration: float) -> Frame:
    """Two bars meeting in the middle. Reads as closing, finishing, joining."""
    out = _blank()
    phase = min(1.0, t / duration)
    half = HEIGHT / 2
    top = int(round(phase * (half - 2)))
    bottom = int(round(HEIGHT - 1 - phase * (half - 2)))
    for y in (top, top + 1, bottom, bottom - 1):
        if 0 <= y < HEIGHT:
            out[y, :] = 255
    return out


def _diverge(t: float, duration: float) -> Frame:
    """Two bars parting from the middle. The inverse of converge - opening."""
    out = _blank()
    phase = min(1.0, t / duration)
    half = HEIGHT / 2
    top = int(round(half - 1 - phase * (half - 1)))
    bottom = int(round(half + phase * (half - 1)))
    for y in (top, top + 1, bottom, bottom - 1):
        if 0 <= y < HEIGHT:
            out[y, :] = 255
    return out


def _bounce(t: float, duration: float) -> Frame:
    """A bar up and back down. Reads as working, in progress, not finished."""
    phase = t / duration
    # Triangle wave: up over the first half, back down over the second.
    travel = phase * 2 if phase < 0.5 else (1.0 - phase) * 2
    return _bar(HEIGHT - np.clip(travel, 0, 1) * (HEIGHT - 4) - 2)


def _drain(t: float, duration: float) -> Frame:
    """Starts full and empties downward. Reads as time running out."""
    out = _blank()
    remaining = 1.0 - min(1.0, t / duration)
    filled = int(round(remaining * HEIGHT))
    if filled:
        out[HEIGHT - filled:, :] = 255
    return out


#: Length of one lub-dub-pause cycle. Fixed rather than scaled to the alert
#: duration: a rhythm is only recognisable if it repeats, and scaling it to fit
#: a single play gives one beat followed by a long silence, which reads as the
#: display having stopped rather than as a pulse.
HEARTBEAT_CYCLE = 1.1


def _heartbeat(t: float, duration: float) -> Frame:
    """Two quick swells then a pause, repeating. Reads as alive, waiting.

    The rhythm carries the recognition, not the shape - it is the only
    irregular pattern here, so it stands out in peripheral vision where a shape
    would not resolve at all.
    """
    out = _blank()
    beat = (t % HEARTBEAT_CYCLE) / HEARTBEAT_CYCLE
    level = 0.0
    for centre in (0.10, 0.32):
        d = abs(beat - centre)
        if d < 0.09:
            level = max(level, 1.0 - d / 0.09)
    out[:, :] = int(np.clip(level, 0, 1) * 255)
    return out


def _flicker(t: float, duration: float) -> Frame:
    """Random static. Reads as something wrong - deliberately unpleasant."""
    # Seeded per frame so it is deterministic but different every frame.
    rng = np.random.default_rng(int(t * 40))
    return (rng.random((HEIGHT, WIDTH)) < 0.45).astype(np.uint8) * 255


ALERTS: dict[str, Alert] = {
    "rise": Alert("rise", _rise, 1.2, "bar sweeps up - success, done"),
    "fall": Alert("fall", _fall, 1.2, "bar sweeps down - failure, stopped"),
    "blink": Alert("blink", _blink, 1.5, "hard flashes - urgent, attention"),
    "pulse": Alert("pulse", _pulse, 2.4, "smooth swell - gentle, informational",
                   mode="greyscale", fps=12.0),
    "fill": Alert("fill", _fill, 1.8, "fills and empties - task complete"),
    "converge": Alert("converge", _converge, 1.4, "bars meet - closing, joining"),
    "diverge": Alert("diverge", _diverge, 1.4, "bars part - opening, starting"),
    "bounce": Alert("bounce", _bounce, 1.6, "bar up and back - working, in progress"),
    "drain": Alert("drain", _drain, 1.8, "empties downward - time running out"),
    "heartbeat": Alert("heartbeat", _heartbeat, 2.2,
                       "two quick swells - alive, waiting",
                       mode="greyscale", fps=12.0),
    "flicker": Alert("flicker", _flicker, 1.2, "random static - something wrong"),
}


def build(name: str, names: list[str], *, repeat: int = 1) -> tuple[Producer, float, str, float]:
    """Producer, total duration, render mode and fps for an alert.

    Every panel shows the same pattern. Splitting a two-second shape across
    panels would make it depend on both being in view, which defeats the point
    of something you are meant to catch peripherally.
    """
    if name not in ALERTS:
        raise KeyError(
            f"unknown alert '{name}'; known: {', '.join(sorted(ALERTS))}"
        )
    alert = ALERTS[name]
    repeat = max(1, min(10, repeat))
    total = alert.duration * repeat

    def produce(t: float):
        # Repeats replay the same shape rather than continuing it, so three
        # rises read as three distinct events rather than one long sweep.
        frame = alert.render(t % alert.duration, alert.duration)
        return {panel: frame for panel in names}

    return produce, total, alert.mode, alert.fps
