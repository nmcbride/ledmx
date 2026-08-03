"""Frame construction helpers.

A frame is always a ``(34, 9)`` uint8 numpy array in standard image row-major
order, so Pillow images and raw ffmpeg output drop in without transposition.
The column-major conversion the wire format wants happens once, in
`ledmx.protocol.greyscale_frame`.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .protocol import HEIGHT, WIDTH

Frame = np.ndarray


def new(value: int = 0) -> Frame:
    return np.full((HEIGHT, WIDTH), value, dtype=np.uint8)


# -- perceptual ------------------------------------------------------------

def _gamma_lut(gamma: float) -> np.ndarray:
    ramp = np.linspace(0.0, 1.0, 256)
    return np.clip(np.power(ramp, gamma) * 255.0, 0, 255).astype(np.uint8)


_LUT_CACHE: dict[float, np.ndarray] = {}


def gamma(frame: Frame, value: float = 2.2) -> Frame:
    """Apply gamma correction.

    LED perceived brightness is markedly non-linear: a raw linear ramp looks
    washed out and top-heavy, with most of the visible change crammed into the
    bottom of the range. Encoding through gamma before display spreads the
    steps out perceptually. Worth applying to anything tonal - video, gradients,
    plasma - and worth skipping for content that is already art-directed.
    """
    lut = _LUT_CACHE.get(value)
    if lut is None:
        lut = _LUT_CACHE[value] = _gamma_lut(value)
    return lut[frame]


# -- images ----------------------------------------------------------------

def from_image(img: Image.Image, mode: str = "cover") -> Frame:
    """Convert a Pillow image to a frame.

    The panel is an extreme 9x34 portrait aspect (roughly 1:3.8), so how source
    material is fitted matters more than usual:

    ``cover``    fill the panel, cropping the overflowing axis (default)
    ``contain``  fit entirely, letterboxing the short axis
    ``stretch``  distort to fill
    """
    img = img.convert("L")

    if mode == "stretch":
        img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
        return np.asarray(img, dtype=np.uint8)

    src_w, src_h = img.size
    scale_cover = max(WIDTH / src_w, HEIGHT / src_h)
    scale_contain = min(WIDTH / src_w, HEIGHT / src_h)
    scale = scale_cover if mode == "cover" else scale_contain

    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    out = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    arr = np.asarray(img, dtype=np.uint8)

    # Centre on both axes, cropping or padding as needed.
    src_y = max(0, (new_h - HEIGHT) // 2)
    src_x = max(0, (new_w - WIDTH) // 2)
    dst_y = max(0, (HEIGHT - new_h) // 2)
    dst_x = max(0, (WIDTH - new_w) // 2)
    copy_h = min(HEIGHT, new_h)
    copy_w = min(WIDTH, new_w)

    out[dst_y:dst_y + copy_h, dst_x:dst_x + copy_w] = arr[
        src_y:src_y + copy_h, src_x:src_x + copy_w
    ]
    return out


# -- text ------------------------------------------------------------------

def _default_font() -> ImageFont.ImageFont:
    return ImageFont.load_default()


def text_strip(
    message: str,
    *,
    vertical: bool = True,
    direction: str = "up",
    font: ImageFont.ImageFont | None = None,
) -> np.ndarray:
    """Render text to a strip for scrolling.

    With ``vertical=True`` the text is rotated so it reads along the panel's
    long axis, giving 34 px of glyph height instead of 9 - the only way text of
    any size is legible here. The returned strip is taller than the panel and
    meant to be scrolled by `scroll_v`.

    `direction` must match how the strip will be scrolled, because the rotation
    and the scroll direction are coupled:

    ``up``    rotate clockwise; reading runs top-to-bottom, like film credits
    ``down``  rotate anticlockwise; reading runs bottom-to-top

    Pairing them the other way round gives letters that arrive in the right
    order but are upside down, or correctly-oriented letters in reverse order.
    Neither is readable, and both look like a rendering bug rather than a
    direction mismatch.

    With ``vertical=False`` the text reads across the 9 px axis, which suits
    very short strings only.
    """
    font = font or _default_font()

    probe = Image.new("L", (1, 1))
    box = ImageDraw.Draw(probe).textbbox((0, 0), message, font=font)
    text_w = max(1, box[2] - box[0])
    text_h = max(1, box[3] - box[1])

    img = Image.new("L", (text_w + 2, text_h + 2), 0)
    ImageDraw.Draw(img).text((1 - box[0], 1 - box[1]), message, fill=255, font=font)

    if vertical:
        # Pillow's rotate() is anticlockwise. Rotating -90 puts the first
        # character at the top of the strip, which is what scrolling up needs;
        # +90 puts it at the bottom, which is what scrolling down needs.
        angle = -90 if direction == "up" else 90
        img = img.rotate(angle, expand=True)
        return _fit_width(np.asarray(img, dtype=np.uint8))

    scale = HEIGHT / img.height if img.height > HEIGHT else 1.0
    if scale < 1.0:
        img = img.resize(
            (max(1, int(round(img.width * scale))), HEIGHT), Image.LANCZOS
        )
    return np.asarray(img, dtype=np.uint8)


def _fit_width(strip: np.ndarray) -> np.ndarray:
    """Fit a strip to the panel width without resampling where possible.

    Rescaling is what ruins small text. A bitmap font's glyphs are a handful of
    pixels across: interpolating them blurs the strokes together, and nearest
    neighbour drops whole pixel rows, breaking letters apart. Neither is
    recoverable at this size.

    But the font's cell is taller than its ink - Pillow's default font uses an
    11 px cell for roughly 7 px of uppercase - so the strip usually fits the
    9 px panel once the blank margins are trimmed. Cropping to the ink and
    centring it leaves every glyph pixel-exact. Only genuinely oversized text
    falls back to resampling.
    """
    ink = np.where(strip.max(axis=0) > 0)[0]
    if ink.size:
        lo, hi = int(ink[0]), int(ink[-1]) + 1
    else:
        lo, hi = 0, strip.shape[1]

    cropped = strip[:, lo:hi]
    ink_width = cropped.shape[1]

    if ink_width <= WIDTH:
        out = np.zeros((strip.shape[0], WIDTH), dtype=np.uint8)
        x = (WIDTH - ink_width) // 2
        out[:, x:x + ink_width] = cropped
        return out

    # Too wide to crop: resample, accepting the quality loss.
    img = Image.fromarray(cropped)
    scale = WIDTH / ink_width
    img = img.resize(
        (WIDTH, max(1, int(round(cropped.shape[0] * scale)))), Image.LANCZOS
    )
    return np.asarray(img, dtype=np.uint8)


# -- transforms ------------------------------------------------------------

def scroll_v(strip: np.ndarray, offset: int, *, wrap: bool = True) -> Frame:
    """Take a HEIGHT-tall window into a taller strip."""
    out = new()
    if strip.size == 0:
        return out

    strip_h, strip_w = strip.shape
    w = min(WIDTH, strip_w)
    x_off = (WIDTH - w) // 2

    if wrap:
        rows = np.arange(offset, offset + HEIGHT) % strip_h
        out[:, x_off:x_off + w] = strip[rows, :w]
        return out

    for y in range(HEIGHT):
        src = offset + y
        if 0 <= src < strip_h:
            out[y, x_off:x_off + w] = strip[src, :w]
    return out


def blend(a: Frame, b: Frame, mode: str = "max", alpha: float = 1.0) -> Frame:
    """Combine two frames.

    ``max`` behaves like additive light without clipping artefacts, which is
    usually what looks right on LEDs; ``over`` is a straight alpha mix.
    """
    if mode == "max":
        return np.maximum(a, (b.astype(np.float32) * alpha).astype(np.uint8))
    if mode == "add":
        return np.clip(
            a.astype(np.int16) + (b.astype(np.float32) * alpha).astype(np.int16),
            0,
            255,
        ).astype(np.uint8)
    if mode == "over":
        return np.clip(
            a.astype(np.float32) * (1 - alpha) + b.astype(np.float32) * alpha,
            0,
            255,
        ).astype(np.uint8)
    raise ValueError(f"unknown blend mode: {mode}")


def percentage(value: float, *, from_bottom: bool = True) -> Frame:
    """A simple fill meter, 0.0-1.0."""
    out = new()
    filled = int(round(max(0.0, min(1.0, value)) * HEIGHT))
    if filled == 0:
        return out
    if from_bottom:
        out[HEIGHT - filled:, :] = 255
    else:
        out[:filled, :] = 255
    return out
