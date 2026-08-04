"""Procedural animation sources.

Each source is constructed with the canvas size it should render at - which is
the *layout's* virtual size, not necessarily one panel - and is then called
with a timestamp to produce a frame. That keeps them agnostic about whether
they're driving one panel, two side by side, or two flanking a keyboard.

The panels are 9x34: extremely tall and narrow. Effects with a strong vertical
axis (rain, fire) read far better here than ones designed for landscape.
"""

from __future__ import annotations

import numpy as np

from ..protocol import HEIGHT, WIDTH

Size = tuple[int, int]  # (height, width)


class Plasma:
    """Classic interference-pattern plasma - smooth, tonal, endless."""

    def __init__(self, size: Size = (HEIGHT, WIDTH), *, speed: float = 1.0,
                 scale: float = 1.0):
        h, w = size
        self.speed = speed
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        self._x = xx / max(1.0, scale)
        self._y = yy / max(1.0, scale)
        self._r = np.sqrt((xx - w / 2) ** 2 + (yy - h / 2) ** 2) / max(1.0, scale)

    def __call__(self, t: float) -> np.ndarray:
        t *= self.speed
        v = (
            np.sin(self._x / 2.0 + t)
            + np.sin(self._y / 4.0 + t * 0.7)
            + np.sin((self._x + self._y) / 5.0 + t * 1.3)
            + np.sin(self._r / 3.0 - t * 1.1)
        )
        v = (v + 4.0) / 8.0  # -4..4 -> 0..1
        return (v * 255.0).astype(np.uint8)


class MatrixRain:
    """Falling drops with fading trails. Suits the portrait aspect exactly.

    Two details matter for this to read as rain rather than as falling dots:

    * **Decay is in real time, not frames.** ``tau`` is the exponential time
      constant in seconds, so a trail looks the same at 6 fps and at 56 fps.
      Decaying per frame instead makes the trail vanish at low frame rates,
      which is where the greyscale mode lives.
    * **Drops paint every row they cross.** At 6 fps a drop advances two or
      three rows per frame; painting only its final position leaves gaps, so
      the trail comes out dotted even before decay touches it.

    In 1-bit mode the threshold sits at 50%, so the visible trail is whatever
    stays above half brightness - about ``tau * 0.7`` seconds of travel. The
    trail is solid rather than fading, but it is still a trail.
    """

    def __init__(self, size: Size = (HEIGHT, WIDTH), *, density: float = 0.5,
                 speed: float = 11.0, tau: float = 0.20, seed: int = 0):
        self.h, self.w = size
        self.speed = speed
        self.tau = tau
        self._rng = np.random.default_rng(seed)
        self._buf = np.zeros((self.h, self.w), dtype=np.float32)
        self._drops = self._rng.uniform(-self.h, 0, size=self.w).astype(np.float32)
        self._rates = self._rng.uniform(0.7, 1.4, size=self.w).astype(np.float32)
        self._active = self._rng.random(self.w) < density
        self._last_t = 0.0

    def __call__(self, t: float) -> np.ndarray:
        # Clamp dt so a stall (or the very first call) doesn't wipe the buffer.
        dt = min(0.2, max(0.0, t - self._last_t))
        self._last_t = t

        self._buf *= float(np.exp(-dt / self.tau))

        previous = self._drops.copy()
        self._drops += self._rates * self.speed * dt

        for x in range(self.w):
            if not self._active[x]:
                continue
            # Fill every row between the old and new head position.
            start = int(np.floor(previous[x])) + 1
            end = int(np.floor(self._drops[x]))
            for y in range(start, end + 1):
                if 0 <= y < self.h:
                    self._buf[y, x] = 1.0

            if self._drops[x] > self.h + self._rng.uniform(0, self.h):
                self._drops[x] = -self._rng.uniform(0, self.h * 0.5)
                self._rates[x] = self._rng.uniform(0.7, 1.4)
                self._active[x] = self._rng.random() < 0.85

        return np.clip(self._buf * 255.0, 0, 255).astype(np.uint8)


#: Which edge (or edges) the fire burns from.
FIRE_SOURCES = ("bottom", "top", "left", "right", "both", "sides")


class Fire:
    """Fire propagating away from one or more edges.

    Orientation matters more here than on a normal display. Fire is legible
    mostly through *sideways* movement - flames splitting, licking, curling -
    and nine columns cannot show any of it, so a bottom-burning fire on this
    panel reads as a vertical shimmer. Burning from a long edge instead gives
    the spread 34 pixels to happen in, which is the only axis with room for it.

    The simulation always runs bottom-up on its own buffer; orientation is
    applied by rotating the result. That keeps one propagation rule rather than
    four, and means a fix to the flame behaviour applies to every direction.
    """

    def __init__(self, size: Size = (HEIGHT, WIDTH), *, source: str = "bottom",
                 cooling: float = 0.11, seed: int = 0):
        if source not in FIRE_SOURCES:
            raise ValueError(
                f"source must be one of {', '.join(FIRE_SOURCES)}, got {source!r}"
            )
        self.h, self.w = size
        self.source = source
        self.cooling = cooling
        self._rng = np.random.default_rng(seed)
        self._last_t = 0.0

        # Sideways sources rotate the working buffer, so the simulation's
        # "width" is the panel's long axis.
        sideways = source in ("left", "right", "sides")
        shape = (self.w, self.h) if sideways else (self.h, self.w)
        self._count = 2 if source in ("both", "sides") else 1
        self._buffers = [np.zeros(shape, dtype=np.float32)
                         for _ in range(self._count)]
        # Per-column embers that drift, so hot spots persist and wander instead
        # of the base re-randomising every frame - uniform seeding is what makes
        # fire shimmer rather than flicker.
        self._embers = [self._rng.uniform(0.55, 1.0, shape[1]).astype(np.float32)
                        for _ in range(self._count)]

    def _advance(self, buf: np.ndarray, embers: np.ndarray) -> np.ndarray:
        embers += self._rng.normal(0.0, 0.18, embers.shape).astype(np.float32)
        # Smooth laterally so a hot spot has width and survives a few frames.
        embers[:] = (
            embers * 0.6
            + np.roll(embers, 1) * 0.2
            + np.roll(embers, -1) * 0.2
        )
        np.clip(embers, 0.35, 1.0, out=embers)
        buf[-1, :] = embers

        up = np.roll(buf, -1, axis=0)
        left = np.roll(up, -1, axis=1)
        right = np.roll(up, 1, axis=1)
        buf = up * 0.56 + left * 0.22 + right * 0.22

        # Cooling varies by column as well as by cell, so some columns carry
        # heat further - the nearest thing to a lick that this width allows.
        column_bias = self._rng.uniform(0.6, 1.4, buf.shape[1]).astype(np.float32)
        buf -= self._rng.uniform(0.0, self.cooling, buf.shape) * column_bias

        # Occasional sparks: a bright cell thrown clear of the flame front.
        if self._rng.random() < 0.35:
            x = self._rng.integers(0, buf.shape[1])
            y = self._rng.integers(buf.shape[0] // 2, buf.shape[0] - 1)
            buf[y, x] = 1.0

        np.clip(buf, 0.0, 1.0, out=buf)
        return buf

    def _orient(self, buf: np.ndarray, index: int) -> np.ndarray:
        """Map a bottom-burning buffer onto the panel for this source."""
        if self.source == "bottom":
            return buf
        if self.source == "top":
            return np.flipud(buf)
        if self.source == "both":
            return buf if index == 0 else np.flipud(buf)
        if self.source == "left":
            return np.rot90(buf, k=-1)
        if self.source == "right":
            return np.rot90(buf, k=1)
        # sides: one from each long edge
        return np.rot90(buf, k=-1) if index == 0 else np.rot90(buf, k=1)

    def __call__(self, t: float) -> np.ndarray:
        # Fixed-step update keeps the look stable regardless of frame rate.
        steps = 1 if t - self._last_t < 0.05 else 2
        self._last_t = t

        for _ in range(steps):
            for i in range(self._count):
                self._buffers[i] = self._advance(self._buffers[i], self._embers[i])

        out = np.zeros((self.h, self.w), dtype=np.float32)
        for i, buf in enumerate(self._buffers):
            out = np.maximum(out, self._orient(buf, i))
        return (out * 255.0).astype(np.uint8)


class Life:
    """Conway's Game of Life with a fade trail and stagnation reseeding."""

    def __init__(self, size: Size = (HEIGHT, WIDTH), *, step_hz: float = 8.0,
                 fill: float = 0.3, seed: int = 0):
        self.h, self.w = size
        self.step_hz = step_hz
        self.fill = fill
        self._rng = np.random.default_rng(seed)
        self._cells = self._rng.random((self.h, self.w)) < fill
        self._trail = np.zeros((self.h, self.w), dtype=np.float32)
        self._next_step = 0.0
        self._history: list[int] = []

    def _step(self) -> None:
        c = self._cells
        neighbours = sum(
            np.roll(np.roll(c, dy, axis=0), dx, axis=1)
            for dy in (-1, 0, 1)
            for dx in (-1, 0, 1)
            if not (dy == 0 and dx == 0)
        )
        self._cells = (neighbours == 3) | (c & (neighbours == 2))

        # Reseed when the population stops changing (still lifes / short cycles),
        # otherwise the display freezes and looks broken.
        pop = int(self._cells.sum())
        self._history.append(pop)
        if len(self._history) > 12:
            self._history.pop(0)
            if pop == 0 or len(set(self._history)) <= 2:
                self._cells = self._rng.random((self.h, self.w)) < self.fill
                self._history.clear()

    def __call__(self, t: float) -> np.ndarray:
        while t >= self._next_step:
            self._step()
            self._next_step += 1.0 / self.step_hz

        self._trail *= 0.75
        self._trail[self._cells] = 1.0
        return (self._trail * 255.0).astype(np.uint8)


class Sweep:
    """A moving bar - deliberately simple, useful for checking geometry.

    Sweeping across a spanned layout is the quickest way to see whether your
    panel ordering and gap are right: the bar should exit one panel and enter
    the next with plausible timing.
    """

    def __init__(self, size: Size = (HEIGHT, WIDTH), *, period: float = 4.0,
                 width: int = 2, vertical: bool = False):
        self.h, self.w = size
        self.period = period
        self.width = width
        self.vertical = vertical

    def __call__(self, t: float) -> np.ndarray:
        out = np.zeros((self.h, self.w), dtype=np.uint8)
        phase = (t % self.period) / self.period
        if self.vertical:
            pos = int(phase * self.h)
            for i in range(self.width):
                out[(pos + i) % self.h, :] = 255
        else:
            pos = int(phase * self.w)
            for i in range(self.width):
                out[:, (pos + i) % self.w] = 255
        return out


SOURCES = {
    "plasma": Plasma,
    "rain": MatrixRain,
    "fire": Fire,
    "life": Life,
    "sweep": Sweep,
}
