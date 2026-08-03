"""Greyscale to 1-bit conversion, spatially and over time.

The panels give you a choice, and it is a stark one:

* Native greyscale costs 10 commands per frame - 8 bits of depth at ~6 fps.
* 1-bit costs 1 command per frame - no depth at all, at ~59 fps.

Neither extreme is much good for moving images. Dithering trades between them:
threshold to 1-bit, but vary the threshold *per sub-frame* so that a pixel lit
for 3 of every 4 sub-frames is perceived as 75% bright. The eye integrates,
and you get apparent tonality at roughly ten times the greyscale frame rate.

The threshold pattern varies spatially (so neighbouring pixels don't switch in
unison, which would read as flicker) and temporally (so each pixel's duty cycle
approximates its intended brightness). A Bayer matrix does both cheaply and,
unlike error diffusion, is stable frame to frame - error diffusion recomputes
its noise pattern every frame, which on a display this small crawls visibly.
"""

from __future__ import annotations

import numpy as np

# Standard 4x4 Bayer threshold matrix, values 0..15.
BAYER_4 = np.array(
    [
        [0, 8, 2, 10],
        [12, 4, 14, 6],
        [3, 11, 1, 9],
        [15, 7, 13, 5],
    ],
    dtype=np.float32,
)

BAYER_2 = np.array([[0, 2], [3, 1]], dtype=np.float32)


def _tile(matrix: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    h, w = shape
    mh, mw = matrix.shape
    return np.tile(matrix, (h // mh + 1, w // mw + 1))[:h, :w]


class OrderedDither:
    """Bayer dithering with a temporal phase offset.

    `phases` sets how many sub-frames one duty cycle spans. **Leave it at 1.**

    Multi-phase temporal dithering was measured on real hardware and does not
    work here: the command budget is ~59 sub-frames per second, so a 4-phase
    cycle completes at ~15 Hz and a 2-phase cycle at ~30 Hz, both far below
    flicker fusion. The result is not perceived tone, it is visible strobing -
    some pixels solid, others blinking. The firmware's own hardware PWM already
    does this correctly, which is why native greyscale looks smooth.

    With ``phases=1`` the threshold pattern is fixed, so every pixel holds a
    stable state and tone comes purely from the spatial pattern. That cannot
    flicker. On a 9x34 panel it reads as halftone rather than as smooth tone,
    which suits photographic content but not gradients.
    """

    def __init__(
        self,
        size: tuple[int, int],
        *,
        phases: int = 1,
        matrix: np.ndarray | None = None,
    ):
        self.size = size
        self.phases = max(1, phases)
        base = _tile(BAYER_4 if matrix is None else matrix, size)
        base = base / (base.max() + 1.0)  # 0..<1

        # Offset the spatial pattern per phase so a pixel's duty cycle spreads
        # evenly across the cycle rather than all pixels toggling together.
        self._thresholds = np.stack(
            [
                (base + (p / self.phases)) % 1.0
                for p in range(self.phases)
            ]
        ).astype(np.float32)

    def __call__(self, frame: np.ndarray, phase: int) -> np.ndarray:
        """Threshold a greyscale frame to bool for the given sub-frame."""
        norm = frame.astype(np.float32) / 255.0
        return norm > self._thresholds[phase % self.phases]


class FloydSteinberg:
    """Error-diffusion dithering - best static quality, no temporal component.

    Produces noticeably better tonality than ordered dithering on a still
    image, at the cost of a Python-level serial loop over 306 pixels and a
    noise pattern that changes completely between frames. Use it for stills;
    prefer `OrderedDither` for anything moving.
    """

    def __init__(self, size: tuple[int, int]):
        self.size = size

    def __call__(self, frame: np.ndarray, phase: int = 0) -> np.ndarray:
        work = frame.astype(np.float32) / 255.0
        h, w = work.shape
        out = np.zeros((h, w), dtype=bool)

        for y in range(h):
            for x in range(w):
                old = work[y, x]
                new = 1.0 if old >= 0.5 else 0.0
                out[y, x] = new > 0.5
                err = old - new
                if x + 1 < w:
                    work[y, x + 1] += err * 7 / 16
                if y + 1 < h:
                    if x > 0:
                        work[y + 1, x - 1] += err * 3 / 16
                    work[y + 1, x] += err * 5 / 16
                    if x + 1 < w:
                        work[y + 1, x + 1] += err * 1 / 16
        return out


class Threshold:
    """Plain 50% threshold - no dithering. Fastest, and right for line art."""

    def __init__(self, size: tuple[int, int], *, level: int = 128):
        self.size = size
        self.level = level

    def __call__(self, frame: np.ndarray, phase: int = 0) -> np.ndarray:
        return frame >= self.level


DITHERERS = {
    "ordered": OrderedDither,
    "floyd": FloydSteinberg,
    "threshold": Threshold,
    "none": Threshold,
}


def make(kind: str, size: tuple[int, int], **kwargs) -> object:
    if kind not in DITHERERS:
        raise ValueError(
            f"unknown dither '{kind}'; choose from {', '.join(sorted(DITHERERS))}"
        )
    if kind == "ordered":
        return OrderedDither(size, **kwargs)
    return DITHERERS[kind](size)
