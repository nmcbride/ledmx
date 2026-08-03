"""A clock: hours on one panel, minutes on the next.

Two digits per panel at 2x scale gives 6x10 glyphs - large enough to read at a
glance across a room, which is the only thing a clock on a status display needs
to do. Splitting hours and minutes across panels means it works identically
whether the modules sit adjacent or flank the keyboard: neither half depends on
the other being nearby.

A seconds bar along the bottom fills over each minute. Without it a clock on a
panel this size is indistinguishable from a frozen display, which matters when
the thing it is mostly proving is that the daemon is still alive.
"""

from __future__ import annotations

import time

import numpy as np

from .. import font
from ..protocol import HEIGHT, WIDTH

Size = tuple[int, int]

DIGIT_LEVEL = 255
SECONDS_LEVEL = 90
SECONDS_LEAD = 200


class ClockPanel:
    """One field of the time - hours or minutes - as two stacked digits."""

    def __init__(
        self,
        field: str,
        size: Size = (HEIGHT, WIDTH),
        *,
        scale: int = 2,
        show_seconds: bool = True,
        hour24: bool = True,
    ):
        if field not in ("hours", "minutes", "full"):
            raise ValueError(
                f"field must be 'hours', 'minutes' or 'full', got {field!r}"
            )
        if field == "full":
            # Four digits have to fit one panel, so they go two per row - and
            # two 6px digits will not fit 9 columns, so the scale drops to 1.
            # Smaller, but self-contained, which is the point.
            scale = 1
        self.h, self.w = size
        self.field = field
        self.scale = scale
        self.show_seconds = show_seconds
        self.hour24 = hour24

        self.digit_h = font.GLYPH_HEIGHT * scale
        self.seconds_h = 2 if show_seconds else 0

        # Centre the pair of digits in the space above the seconds bar.
        block = self.digit_h * 2 + scale  # two digits plus a gap
        usable = self.h - self.seconds_h
        self.top = max(0, (usable - block) // 2)

    def _hours(self, now: time.struct_time) -> str:
        hour = now.tm_hour
        if not self.hour24:
            hour = hour % 12 or 12
        return f"{hour:02d}"

    def _value(self, now: time.struct_time) -> str:
        if self.field == "hours":
            return self._hours(now)
        if self.field == "minutes":
            return f"{now.tm_min:02d}"
        return self._hours(now) + f"{now.tm_min:02d}"

    def __call__(self, t: float) -> np.ndarray:
        now = time.localtime()
        out = np.zeros((self.h, self.w), dtype=np.uint8)

        if self.field == "full":
            # Hours over minutes, two digits per row. Stacking all four in a
            # single column would fit, but reads as a list of digits rather
            # than as a time.
            rows = [self._hours(now), f"{now.tm_min:02d}"]
            block = self.digit_h * 2 + self.scale * 2
            top = max(0, (self.h - self.seconds_h - block) // 2)
            for i, text in enumerate(rows):
                font.draw_centered(
                    out, text, top + i * (self.digit_h + self.scale * 2),
                    level=DIGIT_LEVEL, scale=self.scale,
                )
        else:
            text = self._value(now)
            font.draw_centered(
                out, text[0], self.top, level=DIGIT_LEVEL, scale=self.scale
            )
            font.draw_centered(
                out, text[1], self.top + self.digit_h + self.scale,
                level=DIGIT_LEVEL, scale=self.scale,
            )

        if self.show_seconds:
            # Fractional seconds so the bar advances smoothly rather than
            # stepping once a second - at 6 fps a stepping bar reads as a
            # stutter.
            fraction = (now.tm_sec + (t % 1.0)) / 60.0
            lit = int(fraction * self.w)
            row = self.h - self.seconds_h
            if lit > 0:
                out[row:, :lit] = SECONDS_LEVEL
            if lit < self.w:
                out[row:, lit] = SECONDS_LEAD

        return out


def build_clock(
    names: list[str], size: Size = (HEIGHT, WIDTH), *, contiguous: bool = True
) -> dict:
    """Clock panels, laid out according to whether the modules touch.

    **Contiguous** panels split the time: big 6x10 hours on one, minutes on the
    next. Legible across a room, and the two halves read as one time precisely
    because they are adjacent.

    **Separated** panels each show the whole time, smaller. Splitting it across
    a keyboard-width gap would read as two unrelated two-digit numbers rather
    than as a clock, so redundancy beats size here.
    """
    if len(names) == 1:
        return {names[0]: ClockPanel("full", size)}
    if not contiguous:
        return {name: ClockPanel("full", size) for name in names}
    fields = ["hours", "minutes"]
    return {
        name: ClockPanel(fields[i % 2], size)
        for i, name in enumerate(names)
    }
