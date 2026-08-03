"""Scrolling text.

At 9 px wide, text rendered the obvious way is unreadable. Rotating it so the
reading direction runs along the panel's 34 px axis gives glyphs nearly four
times the height, which is the difference between legible and not.
"""

from __future__ import annotations

import numpy as np
from PIL import ImageFont

from .. import canvas
from ..protocol import HEIGHT, WIDTH


class ScrollingText:
    """Scrolls a message along the panel's long axis.

    `direction` is passed through to the renderer as well as controlling the
    scroll, because glyph rotation and scroll direction have to agree - see
    `ledmx.canvas.text_strip`. Scrolling up reads top-to-bottom; scrolling down
    reads bottom-to-top.
    """

    def __init__(
        self,
        message: str,
        size: tuple[int, int] = (HEIGHT, WIDTH),
        *,
        speed: float = 12.0,
        gap: int = 12,
        font: ImageFont.ImageFont | None = None,
        vertical: bool = True,
        direction: str = "up",
    ):
        if direction not in ("up", "down"):
            raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")
        self.h, self.w = size
        self.speed = speed
        self.direction = direction
        strip = canvas.text_strip(
            message, vertical=vertical, direction=direction, font=font
        )

        # Pad so the message clears the panel before repeating.
        if strip.size:
            pad = np.zeros((gap, strip.shape[1]), dtype=np.uint8)
            strip = np.vstack([strip, pad])
        self.strip = strip

    def __call__(self, t: float) -> np.ndarray:
        out = np.zeros((self.h, self.w), dtype=np.uint8)
        if self.strip.size == 0:
            return out

        strip_h, strip_w = self.strip.shape
        travelled = int(t * self.speed)
        # Scrolling up walks forward through the strip; scrolling down walks
        # backward. Modulo keeps both wrapping cleanly.
        offset = (travelled if self.direction == "up" else -travelled) % strip_h
        rows = np.arange(offset, offset + self.h) % strip_h
        w = min(self.w, strip_w)
        x = (self.w - w) // 2
        out[:, x:x + w] = self.strip[rows, :w]
        return out


class StaticText:
    """A short message held still, centred."""

    def __init__(self, message: str, size: tuple[int, int] = (HEIGHT, WIDTH),
                 *, vertical: bool = True):
        self.h, self.w = size
        strip = canvas.text_strip(message, vertical=vertical)
        out = np.zeros((self.h, self.w), dtype=np.uint8)
        if strip.size:
            copy_h = min(self.h, strip.shape[0])
            copy_w = min(self.w, strip.shape[1])
            y = (self.h - copy_h) // 2
            x = (self.w - copy_w) // 2
            out[y:y + copy_h, x:x + copy_w] = strip[:copy_h, :copy_w]
        self._frame = out

    def __call__(self, t: float) -> np.ndarray:
        return self._frame
